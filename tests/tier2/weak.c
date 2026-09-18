/* Weak externals via /alternatename. weak_hook has no definition anywhere, so it must resolve to
   its alternate; strong_hook is defined in plain.c, so the alternate must be ignored. */
#pragma comment(linker, "/alternatename:weak_hook=weak_hook_default")
#pragma comment(linker, "/alternatename:strong_hook=strong_hook_default")

int weak_hook(void);
int strong_hook(void);

int weak_hook_default(void) { return 17; }
int strong_hook_default(void) { return -1; }

int weak_check(void) { return weak_hook() == 17; }
int strong_check(void) { return strong_hook() == 23; }
