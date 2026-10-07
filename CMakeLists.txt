cmake_minimum_required(VERSION 3.20)
project(libdsk_native C)

if(NOT WIN32 OR NOT MSVC)
    message(FATAL_ERROR "Use the upstream configure script for Linux and Unix builds")
endif()
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
    message(FATAL_ERROR "The native Windows DLL build requires -A Win32")
endif()

set(LIBDSK_LIB_DIR "${CMAKE_CURRENT_SOURCE_DIR}/lib")
file(GLOB LIBDSK_SOURCES CONFIGURE_DEPENDS
    "${LIBDSK_LIB_DIR}/dsk*.c"
    "${LIBDSK_LIB_DIR}/drv*.c"
    "${LIBDSK_LIB_DIR}/blast.c"
    "${LIBDSK_LIB_DIR}/comp*.c"
    "${LIBDSK_LIB_DIR}/crc*.c"
    "${LIBDSK_LIB_DIR}/ldbs.c"
    "${LIBDSK_LIB_DIR}/ldbs2txt.c"
    "${LIBDSK_LIB_DIR}/txt2ldbs.c"
    "${LIBDSK_LIB_DIR}/remote.c"
    "${LIBDSK_LIB_DIR}/rpc*.c")
list(REMOVE_ITEM LIBDSK_SOURCES
    "${LIBDSK_LIB_DIR}/dskjni.c"
    "${LIBDSK_LIB_DIR}/drvdos16.c"
    "${LIBDSK_LIB_DIR}/drvdos32.c"
    "${LIBDSK_LIB_DIR}/drvint25.c"
    "${LIBDSK_LIB_DIR}/drvlinux.c"
    "${LIBDSK_LIB_DIR}/drvntwdm.c"
    "${LIBDSK_LIB_DIR}/drvwin16.c")

add_library(libdsk SHARED ${LIBDSK_SOURCES})
target_include_directories(libdsk BEFORE PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/windows"
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
    "${LIBDSK_LIB_DIR}")
target_compile_definitions(libdsk PRIVATE
    WIN32 LIBDSK_EXPORTS _CRT_SECURE_NO_WARNINGS _CRT_NONSTDC_NO_WARNINGS)
target_compile_options(libdsk PRIVATE /W3)
set_target_properties(libdsk PROPERTIES
    C_STANDARD 99 C_STANDARD_REQUIRED YES OUTPUT_NAME libdsk)

file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/include/libdsk.h" VERSION_LINE
    REGEX "^#define LIBDSK_VERSION ")
string(REGEX REPLACE "^#define LIBDSK_VERSION \"([^\"]+)\".*" "\\1"
    LIBDSK_SOURCE_VERSION "${VERSION_LINE}")
file(WRITE "${CMAKE_BINARY_DIR}/libdsk-build-info.txt"
    "libdsk_source_version=${LIBDSK_SOURCE_VERSION}\n")