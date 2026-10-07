#ifndef NATIVE_LIBDSK_CONFIG_H
#define NATIVE_LIBDSK_CONFIG_H

#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/utime.h>
#include <time.h>
#include <direct.h>
#include <windows.h>

#define HAVE_ASSERT_H 1
#define HAVE_ERRNO_H 1
#define HAVE_FCNTL_H 1
#define HAVE_IO_H 1
#define HAVE_LIMITS_H 1
#define HAVE_STDIO_H 1
#define HAVE_STDLIB_H 1
#define HAVE_STRING_H 1
#define HAVE_SYS_STAT_H 1
#define HAVE_SYS_TYPES_H 1
#define HAVE_SYS_UTIME_H 1
#define HAVE_TIME_H 1
#define HAVE_DIRECT_H 1
#define HAVE_WINDOWS_H 1
#define HAVE_STDINT_H 1
#define HAVE_STDBOOL_H 1

#define NO_FORK 1
#define NO_SIGNALS 1

#ifndef PATH_MAX
#define PATH_MAX MAX_PATH
#endif

#ifndef S_ISDIR
#define S_ISDIR(mode) (((mode) & _S_IFMT) == _S_IFDIR)
#endif
#ifndef S_ISREG
#define S_ISREG(mode) (((mode) & _S_IFMT) == _S_IFREG)
#endif

#endif