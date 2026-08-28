# Hermetic shim for find_package(LibXml2), placed ahead of CMake's builtin FindLibXml2 module by
# prepending cmake/find-shims to CMAKE_MODULE_PATH (see root CMakeLists.txt).
#
# Why this exists: without it, llvm/cmake/config-ix.cmake's find_package(LibXml2 REQUIRED) would
# fall through to CMake's builtin FindLibXml2.cmake, which searches pkg-config and the ambient
# filesystem (system package managers, vcpkg toolchain overlays, stray installs on PATH/registry).
# On a cross-compilation host, or a dev machine with libxml2 installed for something unrelated,
# that search can silently succeed against a libxml2 we do not control -- wrong ABI, wrong CRT
# linkage (a DLL import lib instead of our static archive), or simply an unpinned/unaudited
# version. That would violate the "vendored, statically linked, pinned" contract this repo commits
# to for every third-party dependency. This shim instead asserts that cmake/VendoredLibXml2.cmake
# has already defined LibXml2::LibXml2 (included earlier in root CMakeLists.txt) and republishes
# that target's properties under the legacy LIBXML2_* variable names LLVM's build (and any other
# find_package(LibXml2)-based consumer) expects -- no filesystem search, no network, no surprises.

if(NOT TARGET LibXml2::LibXml2)
  message(FATAL_ERROR
    "cmake/find-shims/FindLibXml2.cmake was reached before LibXml2::LibXml2 was defined. "
    "cmake/VendoredLibXml2.cmake must be include()-d in root CMakeLists.txt before "
    "find_package(LibXml2) is ever invoked (directly, or transitively via "
    "llvm/cmake/config-ix.cmake).")
endif()

get_target_property(LIBXML2_INCLUDE_DIR LibXml2::LibXml2 INTERFACE_INCLUDE_DIRECTORIES)
get_target_property(LIBXML2_LIBRARY LibXml2::LibXml2 IMPORTED_LOCATION)

set(LibXml2_FOUND TRUE)
set(LIBXML2_FOUND TRUE)
set(LIBXML2_INCLUDE_DIRS "${LIBXML2_INCLUDE_DIR}")
set(LIBXML2_LIBRARIES "${LIBXML2_LIBRARY}")
# Matches INTERFACE_COMPILE_DEFINITIONS on LibXml2::LibXml2 (cmake/VendoredLibXml2.cmake); anything
# that consumes LIBXML2_DEFINITIONS instead of the imported target still needs LIBXML_STATIC to see
# plain (non-dllimport) declarations from libxml/xmlexports.h.
set(LIBXML2_DEFINITIONS "-DLIBXML_STATIC")
set(LIBXML2_VERSION_STRING "2.15.3")

mark_as_advanced(LIBXML2_INCLUDE_DIR LIBXML2_LIBRARY)
