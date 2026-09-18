/* Plain C translation unit with C linkage. */
int strong_hook(void) { return 23; }

int c_sum(const int *values, int count) {
  int total = 0;
  for (int i = 0; i < count; ++i)
    total += values[i];
  return total;
}
