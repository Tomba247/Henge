// For tests/test_tl_throw.c: exceptions, caught and not, on the threads a program has.
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <thread>
#include <dispatch/dispatch.h>
#include <unistd.h>
int main(int c, char **v) {
    const char *how = c > 1 ? v[1] : "main";
    try { throw std::runtime_error("caught"); } catch (std::exception &e) { printf("%s\n", e.what()); }
    if (!strcmp(how, "main")) throw std::runtime_error("from main");
    if (!strcmp(how, "thread")) { std::thread t([] { throw std::runtime_error("from a thread"); }); t.join(); }
    if (!strcmp(how, "queue")) { dispatch_async(dispatch_get_main_queue(), ^{ throw std::runtime_error("from the main queue"); }); sleep(20); }
    if (!strcmp(how, "noexcept")) [&]() noexcept { throw 5; }();
    puts("end");
    return 0;
}
