// Static TLS (__declspec(thread)) including an over-aligned object, so the linker must build the
// TLS directory and template with the right size and alignment characteristics.
#include "common.h"

struct alignas(64) AlignedBlock {
  int values[16];
};

__declspec(thread) AlignedBlock t_block = {{5, 6, 7}};
__declspec(thread) int t_counter = 11;

int tls_check() {
  ++t_counter;
  t_block.values[15] = t_counter;
  return t_block.values[0] == 5 && t_block.values[2] == 7 && t_counter == 12 &&
         t_block.values[15] == 12;
}
