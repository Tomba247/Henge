// SPDX-License-Identifier: GPL-2.0-or-later
/* vkmap (tools/linux/make-ubuntu-disk.sh --vulkan puts it in the image as henge-vkmap): the smallest thing Vulkan does with memory the CPU can see: a device, host-visible memory, mapped, written,
   read back, a buffer bound to it, a queue submit and a fence. No window. Prints one line per step. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#define CHECK(what, r)                                 \
    do {                                               \
        VkResult r_ = (r);                             \
        printf("vkmap: %s -> %d\n", what, (int)r_);    \
        if (r_ != VK_SUCCESS) return 1;                \
    } while (0)

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    VkDeviceSize size = argc > 1 ? strtoull(argv[1], NULL, 0) : 16384;
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    VkInstance inst;
    CHECK("instance", vkCreateInstance(&ici, NULL, &inst));
    uint32_t n = 8;
    VkPhysicalDevice pds[8];
    vkEnumeratePhysicalDevices(inst, &n, pds);
    if (!n) return printf("vkmap: no devices\n"), 1;
    VkPhysicalDevice pd = pds[0];
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(pd, &props);
    printf("vkmap: device %s\n", props.deviceName);
    float prio = 1;
    VkDeviceQueueCreateInfo qci = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = 0, .queueCount = 1, .pQueuePriorities = &prio};
    VkDeviceCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci};
    VkDevice dev;
    CHECK("device", vkCreateDevice(pd, &dci, NULL, &dev));
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    uint32_t type = UINT32_MAX;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
        if ((mp.memoryTypes[i].propertyFlags & (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) ==
            (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) {
            type = i;
            break;
        }
    printf("vkmap: memory type %u of %u, %llu bytes\n", type, mp.memoryTypeCount, (unsigned long long)size);
    VkMemoryAllocateInfo mai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = size, .memoryTypeIndex = type};
    VkDeviceMemory mem;
    CHECK("allocate", vkAllocateMemory(dev, &mai, NULL, &mem));
    void *p = NULL;
    CHECK("map", vkMapMemory(dev, mem, 0, VK_WHOLE_SIZE, 0, &p));
    memset(p, 0x5a, size);
    printf("vkmap: wrote, reads back %#x\n", ((unsigned char *)p)[size - 1]);
    VkBufferCreateInfo bci = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size, .usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT};
    VkBuffer a, b;
    CHECK("buffer", vkCreateBuffer(dev, &bci, NULL, &a));
    CHECK("bind", vkBindBufferMemory(dev, a, mem, 0));
    /* A second memory and buffer, filled by the device from the first: the host's Vulkan saw what we wrote. */
    VkDeviceMemory mem2;
    CHECK("allocate 2", vkAllocateMemory(dev, &mai, NULL, &mem2));
    void *q = NULL;
    CHECK("map 2", vkMapMemory(dev, mem2, 0, VK_WHOLE_SIZE, 0, &q));
    CHECK("buffer 2", vkCreateBuffer(dev, &bci, NULL, &b));
    CHECK("bind 2", vkBindBufferMemory(dev, b, mem2, 0));
    VkQueue queue;
    vkGetDeviceQueue(dev, 0, 0, &queue);
    VkCommandPoolCreateInfo cpi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = 0};
    VkCommandPool pool;
    CHECK("pool", vkCreateCommandPool(dev, &cpi, NULL, &pool));
    VkCommandBufferAllocateInfo cai = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = pool, .commandBufferCount = 1};
    VkCommandBuffer cb;
    CHECK("command buffer", vkAllocateCommandBuffers(dev, &cai, &cb));
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    vkBeginCommandBuffer(cb, &bi);
    VkBufferCopy region = {.size = size};
    vkCmdCopyBuffer(cb, a, b, 1, &region);
    vkEndCommandBuffer(cb);
    VkFenceCreateInfo fci = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence fence;
    CHECK("fence", vkCreateFence(dev, &fci, NULL, &fence));
    VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cb};
    CHECK("submit", vkQueueSubmit(queue, 1, &si, fence));
    CHECK("wait", vkWaitForFences(dev, 1, &fence, VK_TRUE, 10000000000ull));
    printf("vkmap: the device's copy reads %#x (want 0x5a): %s\n", ((unsigned char *)q)[size - 1], ((unsigned char *)q)[size - 1] == 0x5a ? "ok" : "WRONG");
    return ((unsigned char *)q)[size - 1] == 0x5a ? 0 : 1;
}
