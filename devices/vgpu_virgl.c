// SPDX-License-Identifier: GPL-2.0-or-later
/* virgl 3D for virtio-gpu: see vgpu_virgl.h. */
#include "vgpu_virgl.h"

#include <stdlib.h>

#ifndef HV_VIRGL

HvVgpuVirgl *hv_vgpu_virgl_create(HvVgpu3d *gl, int gles) {
    (void)gl, (void)gles;
    return NULL;
}
void hv_vgpu_virgl_bind(HvVgpuVirgl *v, HvVirtioGpu *g) { (void)v, (void)g; }
void hv_vgpu_virgl_destroy(HvVgpuVirgl *v) { (void)v; }

#else

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/uio.h>
#include <time.h>
#include <virglrenderer.h>

/* virglrenderer only declares it: x, y, z, w, h, d, as the virtio-gpu box (QEMU passes that the same way). */
struct virgl_box {
    uint32_t x, y, z, w, h, d;
};

struct HvVgpuVirgl {
    pthread_t thread;
    pthread_mutex_t mu;
    pthread_cond_t wake, done_cv;
    void (*job)(HvVgpuVirgl *, void *);
    void *arg;
    int job_done, stopping, started, ok, gles;
    pthread_mutex_t call_mu; /* one caller at a time */
    HvVirtioGpu *gpu;
    _Atomic uint64_t last_fence; /* the newest fence asked for, all 64 bits */
    _Atomic int fences_out;
    uint8_t *tmp;
    size_t tmp_cap;
    int log;
};

/* --- the render thread --- */

static void write_fence(void *cookie, uint32_t fence) {
    HvVgpuVirgl *v = cookie;
    /* virglrenderer keeps 32 bits of the guest's 64-bit fence ids: put the rest back. */
    uint64_t last = atomic_load(&v->last_fence);
    uint64_t full = (last & ~0xffffffffull) | fence;
    if (full > last) full -= 1ull << 32;
    atomic_store(&v->fences_out, 0);
    if (v->gpu) hv_virtio_gpu_fence_done(v->gpu, 0, 0, full);
}

static void log_cb(enum virgl_log_level_flags level, const char *message, void *user) {
    (void)level, (void)user;
    fprintf(stderr, "virgl: %s", message);
}

static void *render_thread(void *arg) {
    HvVgpuVirgl *v = arg;
    static struct virgl_renderer_callbacks cbs = {.version = 1, .write_fence = write_fence};
    int flags = VIRGL_RENDERER_USE_EGL | VIRGL_RENDERER_USE_SURFACELESS | (v->gles ? VIRGL_RENDERER_USE_GLES : 0);
    if (v->log) virgl_set_log_callback(log_cb, NULL, NULL);
    int r = virgl_renderer_init(v, flags, &cbs);
    pthread_mutex_lock(&v->mu);
    v->ok = r == 0;
    v->started = 1;
    pthread_cond_broadcast(&v->done_cv);
    if (r != 0) {
        fprintf(stderr, "virgl: virgl_renderer_init failed (%d)\n", r);
        pthread_mutex_unlock(&v->mu);
        return NULL;
    }
    while (!v->stopping) {
        if (!v->job) {
            /* Idle: retire fences every millisecond while any are out, else sleep until called. */
            if (atomic_load(&v->fences_out)) {
                struct timespec ts;
                clock_gettime(CLOCK_REALTIME, &ts);
                ts.tv_nsec += 1000000;
                if (ts.tv_nsec >= 1000000000) ts.tv_sec++, ts.tv_nsec -= 1000000000;
                pthread_cond_timedwait(&v->wake, &v->mu, &ts);
            } else {
                pthread_cond_wait(&v->wake, &v->mu);
            }
        }
        if (v->job) {
            void (*job)(HvVgpuVirgl *, void *) = v->job;
            void *a = v->arg;
            pthread_mutex_unlock(&v->mu);
            job(v, a);
            pthread_mutex_lock(&v->mu);
            v->job = NULL;
            v->job_done = 1;
            pthread_cond_broadcast(&v->done_cv);
        }
        if (atomic_load(&v->fences_out)) {
            pthread_mutex_unlock(&v->mu);
            virgl_renderer_poll();
            pthread_mutex_lock(&v->mu);
        }
    }
    pthread_mutex_unlock(&v->mu);
    virgl_renderer_cleanup(v);
    return NULL;
}

/* Runs job(arg) on the render thread and waits for it. */
static void call(HvVgpuVirgl *v, void (*job)(HvVgpuVirgl *, void *), void *arg) {
    pthread_mutex_lock(&v->call_mu);
    pthread_mutex_lock(&v->mu);
    v->job = job, v->arg = arg, v->job_done = 0;
    pthread_cond_signal(&v->wake);
    while (!v->job_done) pthread_cond_wait(&v->done_cv, &v->mu);
    pthread_mutex_unlock(&v->mu);
    pthread_mutex_unlock(&v->call_mu);
}

/* --- the renderer interface, each call marshalled to the render thread --- */

typedef struct Args {
    uint32_t u[12];
    uint64_t u64;
    const void *p;
    void *out;
    void **iov_base;
    uint32_t *iov_len;
    int r;
} Args;

static void j_capset_info(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    uint32_t id = a->u[0] + 1; /* capsets 1 (VIRGL) and 2 (VIRGL2) */
    uint32_t ver = 0, size = 0;
    virgl_renderer_get_cap_set(id, &ver, &size);
    a->u[1] = id, a->u[2] = ver, a->u[3] = size;
    a->r = size ? 0 : -1;
}
static int capset_info(void *o, uint32_t index, uint32_t *id, uint32_t *max_version, uint32_t *max_size) {
    Args a = {.u = {index}};
    if (index >= 2) return -1;
    call(o, j_capset_info, &a);
    *id = a.u[1], *max_version = a.u[2], *max_size = a.u[3];
    return a.r;
}

static void j_capset(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    virgl_renderer_fill_caps(a->u[0], a->u[1], a->out);
}
static int capset(void *o, uint32_t id, uint32_t version, void *out, uint32_t size) {
    (void)size;
    Args a = {.u = {id, version}, .out = out};
    call(o, j_capset, &a);
    return 0;
}

static void j_ctx_create(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    a->r = a->u[1] ? virgl_renderer_context_create_with_flags(a->u[0], a->u[1], a->u[2], a->p)
                   : virgl_renderer_context_create(a->u[0], a->u[2], a->p);
}
static int ctx_create(void *o, uint32_t ctx, uint32_t context_init, const char *name, uint32_t name_len) {
    Args a = {.u = {ctx, context_init, name_len}, .p = name};
    call(o, j_ctx_create, &a);
    if (((HvVgpuVirgl *)o)->log) fprintf(stderr, "virgl: context %u (%.*s) -> %d\n", ctx, (int)name_len, name ? name : "", a.r);
    return a.r;
}

static void j_ctx_destroy(HvVgpuVirgl *v, void *a_) {
    (void)v;
    virgl_renderer_context_destroy(((Args *)a_)->u[0]);
}
static void ctx_destroy(void *o, uint32_t ctx) {
    Args a = {.u = {ctx}};
    call(o, j_ctx_destroy, &a);
}

static void j_ctx_attach(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    if (a->u[2]) virgl_renderer_ctx_attach_resource((int)a->u[0], (int)a->u[1]);
    else virgl_renderer_ctx_detach_resource((int)a->u[0], (int)a->u[1]);
}
static void ctx_attach(void *o, uint32_t ctx, uint32_t res) {
    Args a = {.u = {ctx, res, 1}};
    call(o, j_ctx_attach, &a);
}
static void ctx_detach(void *o, uint32_t ctx, uint32_t res) {
    Args a = {.u = {ctx, res, 0}};
    call(o, j_ctx_attach, &a);
}

static void j_create_3d(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    struct virgl_renderer_resource_create_args c = {
        .handle = a->u[0], .target = a->u[1], .format = a->u[2], .bind = a->u[3], .width = a->u[4], .height = a->u[5],
        .depth = a->u[6], .array_size = a->u[7], .last_level = a->u[8], .nr_samples = a->u[9], .flags = a->u[10]};
    a->r = virgl_renderer_resource_create(&c, NULL, 0);
}
static int resource_create_3d(void *o, uint32_t res, const uint32_t args[10]) {
    Args a = {.u = {res}};
    memcpy(a.u + 1, args, 10 * sizeof *args);
    call(o, j_create_3d, &a);
    return a.r;
}

static void j_unref(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    struct iovec *iov = NULL;
    int n = 0;
    virgl_renderer_resource_detach_iov((int)a->u[0], &iov, &n);
    free(iov);
    virgl_renderer_resource_unref(a->u[0]);
}
static void resource_unref(void *o, uint32_t res) {
    Args a = {.u = {res}};
    call(o, j_unref, &a);
}

static void j_attach(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    uint32_t n = a->u[1];
    struct iovec *iov = calloc(n, sizeof *iov); /* virglrenderer keeps it until detached */
    if (!iov) {
        a->r = -ENOMEM;
        return;
    }
    for (uint32_t i = 0; i < n; i++) iov[i].iov_base = a->iov_base[i], iov[i].iov_len = a->iov_len[i];
    a->r = virgl_renderer_resource_attach_iov((int)a->u[0], iov, (int)n);
    if (a->r) free(iov);
}
static int attach_backing(void *o, uint32_t res, void **iov_base, uint32_t *iov_len, uint32_t n) {
    Args a = {.u = {res, n}, .iov_base = iov_base, .iov_len = iov_len};
    call(o, j_attach, &a);
    return a.r;
}

static void j_detach(HvVgpuVirgl *v, void *a_) {
    (void)v;
    struct iovec *iov = NULL;
    int n = 0;
    virgl_renderer_resource_detach_iov((int)((Args *)a_)->u[0], &iov, &n);
    free(iov);
}
static void detach_backing(void *o, uint32_t res) {
    Args a = {.u = {res}};
    call(o, j_detach, &a);
}

static void j_transfer(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    struct virgl_box box;
    memcpy(&box, a->p, sizeof box);
    if (a->u[0])
        a->r = virgl_renderer_transfer_write_iov(a->u[2], a->u[1], (int)a->u[3], a->u[4], a->u[5], &box, a->u64, NULL, 0);
    else
        a->r = virgl_renderer_transfer_read_iov(a->u[2], a->u[1], a->u[3], a->u[4], a->u[5], &box, a->u64, NULL, 0);
}
static int transfer(void *o, int to_host, uint32_t ctx, uint32_t res, const uint32_t box[6], uint32_t level, uint32_t stride,
                    uint32_t layer_stride, uint64_t offset) {
    Args a = {.u = {(uint32_t)to_host, ctx, res, level, stride, layer_stride}, .u64 = offset, .p = box};
    call(o, j_transfer, &a);
    return a.r;
}

static void j_submit(HvVgpuVirgl *v, void *a_) {
    (void)v;
    Args *a = a_;
    a->r = virgl_renderer_submit_cmd(a->out, (int)a->u[0], (int)(a->u[1] / 4));
}
static int submit(void *o, uint32_t ctx, void *cmd, uint32_t size) {
    Args a = {.u = {ctx, size}, .out = cmd};
    call(o, j_submit, &a);
    if (a.r && ((HvVgpuVirgl *)o)->log) fprintf(stderr, "virgl: submit %u bytes on context %u -> %d\n", size, ctx, a.r);
    return a.r;
}

static void j_fence(HvVgpuVirgl *v, void *a_) {
    Args *a = a_;
    atomic_store(&v->last_fence, a->u64);
    atomic_store(&v->fences_out, 1);
    a->r = virgl_renderer_create_fence((int)(uint32_t)a->u64, a->u[0]);
}
static int create_fence(void *o, uint32_t ctx, uint32_t ring, uint64_t fence) {
    (void)ring;
    Args a = {.u = {ctx}, .u64 = fence};
    call(o, j_fence, &a);
    return a.r;
}

/* The screen: a rectangle of the scanned-out texture read back, top row first, as BGRA. */
static void j_read(HvVgpuVirgl *v, void *a_) {
    Args *a = a_;
    uint32_t res = a->u[0], x = a->u[1], y = a->u[2], w = a->u[3], h = a->u[4], out_stride = a->u[5];
    struct virgl_renderer_resource_info info;
    if (virgl_renderer_resource_get_info((int)res, &info) != 0) {
        a->r = -1;
        return;
    }
    size_t bytes = (size_t)w * h * 4;
    if (bytes > v->tmp_cap) {
        uint8_t *bigger = realloc(v->tmp, bytes);
        if (!bigger) {
            a->r = -1;
            return;
        }
        v->tmp = bigger, v->tmp_cap = bytes;
    }
    struct virgl_box box = {x, y, 0, w, h, 1};
    struct iovec iov = {v->tmp, bytes};
    a->r = virgl_renderer_transfer_read_iov(res, 0, 0, w * 4, (uint32_t)bytes, &box, 0, &iov, 1);
    if (a->r) return;
    uint8_t *out = a->out;
    for (uint32_t row = 0; row < h; row++) hv_vgpu_to_bgra(info.virgl_format, v->tmp + (size_t)row * w * 4, out + (size_t)row * out_stride, w);
}
static int read_pixels(void *o, uint32_t res, uint32_t x, uint32_t y, uint32_t w, uint32_t h, uint8_t *out, uint32_t out_stride) {
    Args a = {.u = {res, x, y, w, h, out_stride}, .out = out};
    call(o, j_read, &a);
    return a.r;
}

HvVgpuVirgl *hv_vgpu_virgl_create(HvVgpu3d *gl, int gles) {
    HvVgpuVirgl *v = calloc(1, sizeof *v);
    if (!v) return NULL;
    v->gles = gles;
    v->log = getenv("HV_VIRGL_LOG") != NULL;
    pthread_mutex_init(&v->mu, NULL);
    pthread_mutex_init(&v->call_mu, NULL);
    pthread_cond_init(&v->wake, NULL);
    pthread_cond_init(&v->done_cv, NULL);
    if (pthread_create(&v->thread, NULL, render_thread, v) != 0) {
        free(v);
        return NULL;
    }
    pthread_mutex_lock(&v->mu);
    while (!v->started) pthread_cond_wait(&v->done_cv, &v->mu);
    int ok = v->ok;
    pthread_mutex_unlock(&v->mu);
    if (!ok) {
        pthread_join(v->thread, NULL);
        free(v);
        return NULL;
    }
    memset(gl, 0, sizeof *gl);
    gl->opaque = v;
    gl->ncapsets = 2;
    gl->capset_info = capset_info;
    gl->capset = capset;
    gl->ctx_create = ctx_create;
    gl->ctx_destroy = ctx_destroy;
    gl->ctx_attach = ctx_attach;
    gl->ctx_detach = ctx_detach;
    gl->resource_create_3d = resource_create_3d;
    gl->resource_unref = resource_unref;
    gl->attach_backing = attach_backing;
    gl->detach_backing = detach_backing;
    gl->transfer = transfer;
    gl->submit = submit;
    gl->create_fence = create_fence;
    gl->read_pixels = read_pixels;
    fprintf(stderr, "virgl: renderer started (%s)\n", gles ? "OpenGL ES" : "OpenGL");
    return v;
}

void hv_vgpu_virgl_bind(HvVgpuVirgl *v, HvVirtioGpu *g) {
    if (v) v->gpu = g;
}

void hv_vgpu_virgl_destroy(HvVgpuVirgl *v) {
    if (!v) return;
    pthread_mutex_lock(&v->mu);
    v->stopping = 1;
    pthread_cond_signal(&v->wake);
    pthread_mutex_unlock(&v->mu);
    pthread_join(v->thread, NULL);
    free(v->tmp);
    free(v);
}

#endif
