/* For tests/test_tl_throw.c: a library that adds methods to the system's classes, as MoltenVK does to Metal's. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
static int calls;
@interface MTLSamplerDescriptor (Henge)
@property(nonatomic) int hengeMVK;
+ (int)hengeClass;
@end
@implementation MTLSamplerDescriptor (Henge)
- (void)setHengeMVK:(int)v { calls += v; }
- (int)hengeMVK { return calls; }
+ (int)hengeClass { return 7; }
- (NSString *)description { return @"not to be used"; }
@end
int category_check(void);
int category_check(void) {
    MTLSamplerDescriptor *d = [MTLSamplerDescriptor new];
    d.hengeMVK = 40;
    return d.hengeMVK + [MTLSamplerDescriptor hengeClass] - ([d.description isEqual:@"not to be used"] ? 1 : 0);
}
