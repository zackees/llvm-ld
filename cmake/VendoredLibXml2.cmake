# Builds a vendored, statically linked libxml2 in-tree and exposes it as the imported target
# `LibXml2::LibXml2`. This is consumed by llvm/cmake/config-ix.cmake's `find_package(LibXml2
# REQUIRED)` (via cmake/find-shims/FindLibXml2.cmake) so that LLVM's WindowsManifest library can
# merge Windows manifests in-process instead of shelling out to mt.exe. See CMakeLists.txt (root)
# for the surrounding LLVM_ENABLE_LIBXML2 / HAVE_LIBXML2 wiring and why it is required.
#
# Zero upstream LLVM files are patched by this file: it only produces a target that satisfies
# LLVM's existing find_package(LibXml2) contract.

include(CheckIncludeFile)
include(CheckSymbolExists)
include(CheckCSourceCompiles)

if(NOT LLVM_LD_LIBXML2_ROOT)
  message(FATAL_ERROR
    "LLVM_LD_LIBXML2_ROOT is not set. It must point at the extracted pinned libxml2 source tree "
    "(expected to be produced by cmake/Import.cmake before this file is included).")
endif()

set(_llvm_ld_xml2_required_sources
  buf.c chvalid.c dict.c encoding.c entities.c error.c globals.c hash.c list.c parser.c
  parserInternals.c SAX2.c threads.c tree.c uri.c valid.c xmlIO.c xmlmemory.c xmlstring.c
  xmlsave.c)

set(_llvm_ld_xml2_sources)
foreach(_llvm_ld_xml2_src ${_llvm_ld_xml2_required_sources})
  set(_llvm_ld_xml2_src_path "${LLVM_LD_LIBXML2_ROOT}/${_llvm_ld_xml2_src}")
  if(NOT EXISTS "${_llvm_ld_xml2_src_path}")
    message(FATAL_ERROR
      "Vendored libxml2 source '${_llvm_ld_xml2_src}' is missing under LLVM_LD_LIBXML2_ROOT="
      "'${LLVM_LD_LIBXML2_ROOT}'. The extracted archive may be incomplete or the pinned version "
      "may have changed layout.")
  endif()
  list(APPEND _llvm_ld_xml2_sources "${_llvm_ld_xml2_src_path}")
endforeach()

set(_llvm_ld_xml2_gen_dir "${CMAKE_CURRENT_BINARY_DIR}/llvm_ld_xml2_generated")
file(MAKE_DIRECTORY "${_llvm_ld_xml2_gen_dir}/libxml")

# --- config.h -----------------------------------------------------------------------------------
# Platform probes config.h.cmake.in needs. Cross-compiling to Windows via clang-cl (see
# cmake/WinMsvcCross.cmake), so getentropy/glob/mmap declarations do not exist; stdint.h does.
# dlopen/shl_load/readline/history/XML_SYSCONFDIR/XML_THREAD_LOCAL are intentionally left
# undefined -- we exclude the modules (dso loading) and platform facilities (syslog config dir,
# TLS) that would need them, per the deliberately-excluded source list above.
check_include_file("stdint.h" HAVE_STDINT_H)

check_symbol_exists(getentropy "unistd.h" _llvm_ld_have_getentropy)
if(_llvm_ld_have_getentropy)
  set(HAVE_DECL_GETENTROPY 1)
else()
  set(HAVE_DECL_GETENTROPY 0)
endif()

check_symbol_exists(glob "glob.h" _llvm_ld_have_glob)
if(_llvm_ld_have_glob)
  set(HAVE_DECL_GLOB 1)
else()
  set(HAVE_DECL_GLOB 0)
endif()

check_symbol_exists(mmap "sys/mman.h" _llvm_ld_have_mmap)
if(_llvm_ld_have_mmap)
  set(HAVE_DECL_MMAP 1)
else()
  set(HAVE_DECL_MMAP 0)
endif()

check_c_source_compiles(
  "__attribute__((destructor)) void llvm_ld_xml2_dtor(void) {}
   int main(void) { return 0; }"
  HAVE_FUNC_ATTRIBUTE_DESTRUCTOR)

# Left undefined on purpose (see comment above): HAVE_DLOPEN, HAVE_LIBHISTORY, HAVE_LIBREADLINE,
# HAVE_SHLLOAD, XML_SYSCONFDIR, XML_THREAD_LOCAL.

configure_file(
  "${LLVM_LD_LIBXML2_ROOT}/config.h.cmake.in"
  "${_llvm_ld_xml2_gen_dir}/config.h"
  @ONLY)

# --- libxml/xmlversion.h --------------------------------------------------------------------
# Feature switches: only output, threads and push parsing are compiled in (see the source list
# above -- no html/catalog/xpath/xinclude/reader/writer/regexp/schemas/relaxng/c14n/nanohttp/
# xmlmodule/pattern/schematron/debugXML/xlink/xpointer objects are built), so every other WITH_*
# must be 0 or the generated header would advertise API surface with no corresponding definitions.
set(VERSION "2.15.3")
set(LIBXML_VERSION_NUMBER 21503)
set(LIBXML_VERSION_EXTRA "")

set(WITH_THREADS 1)
set(WITH_THREAD_ALLOC 0)
set(WITH_OUTPUT 1)
set(WITH_PUSH 1)
set(WITH_READER 0)
set(WITH_PATTERN 0)
set(WITH_WRITER 0)
set(WITH_SAX1 0)
set(WITH_HTTP 0)
# valid.c is compiled in, but LIBXML_VALID_ENABLED only guards the DTD-validation-proper entry
# points in include/libxml/valid.h (xmlValidate*, xmlValidCtxt, ...); the unconditional ID/Ref/
# notation/element/attribute-table management functions parser.c and tree.c call are declared
# outside that guard, so WITH_VALID=0 (matching every other unused WITH_*) still compiles cleanly.
set(WITH_VALID 0)
set(WITH_HTML 0)
set(WITH_C14N 0)
set(WITH_CATALOG 0)
set(WITH_XPATH 0)
set(WITH_XPTR 0)
set(WITH_XINCLUDE 0)
set(WITH_ICONV 0)
set(WITH_ICU 0)
set(WITH_ISO8859X 0)
set(WITH_DEBUG 0)
set(WITH_REGEXPS 0)
set(WITH_RELAXNG 0)
set(WITH_SCHEMAS 0)
set(WITH_SCHEMATRON 0)
set(WITH_MODULES 0)
set(MODULE_EXTENSION "")
set(WITH_ZLIB 0)

configure_file(
  "${LLVM_LD_LIBXML2_ROOT}/include/libxml/xmlversion.h.in"
  "${_llvm_ld_xml2_gen_dir}/libxml/xmlversion.h"
  @ONLY)

# --- the object library ---------------------------------------------------------------------
add_library(llvm_ld_xml2 STATIC ${_llvm_ld_xml2_sources})
set_target_properties(llvm_ld_xml2 PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(llvm_ld_xml2 PRIVATE
  "${LLVM_LD_LIBXML2_ROOT}/include"
  "${_llvm_ld_xml2_gen_dir}")
# LIBXML_STATIC must be defined here (on the objects) too, not only in INTERFACE_COMPILE_DEFINITIONS
# below: include/libxml/xmlexports.h makes XMLPUBFUN expand to __declspec(dllexport) unless
# LIBXML_STATIC is visible while compiling libxml2's own .c files, which would produce a DLL-style
# export table for objects that are never linked into a DLL.
target_compile_definitions(llvm_ld_xml2 PRIVATE LIBXML_STATIC)
if(MSVC)
  target_compile_options(llvm_ld_xml2 PRIVATE /w) # third-party code: suppress warnings, not fix them
else()
  target_compile_options(llvm_ld_xml2 PRIVATE -w)
endif()

# Pin the archive to one fixed directory, identically for every configuration. This matters
# because the IMPORTED_LOCATION below must be a literal path, not a generator expression (see
# comment below): llvm-project/llvm/lib/WindowsManifest/CMakeLists.txt reads the LOCATION property
# with get_property() at *configure* time, which returns IMPORTED_LOCATION's raw string without
# evaluating generator expressions. Left to CMake's per-generator defaults, multi-config generators
# (Visual Studio, Xcode) would nest the output under a per-config subdirectory we can't name yet at
# configure time; setting ARCHIVE_OUTPUT_DIRECTORY[_<CONFIG>] explicitly removes that nesting so a
# single literal path is valid for every configuration.
set(_llvm_ld_xml2_out_dir "${CMAKE_CURRENT_BINARY_DIR}/llvm_ld_xml2_lib")
set_target_properties(llvm_ld_xml2 PROPERTIES ARCHIVE_OUTPUT_DIRECTORY "${_llvm_ld_xml2_out_dir}")

# --- the imported target LLVM's find_package(LibXml2) resolves to ----------------------------
# Must be a real STATIC IMPORTED GLOBAL target with a non-empty IMPORTED_LOCATION, NOT an ALIAS:
# llvm-project/llvm/lib/WindowsManifest/CMakeLists.txt does
# `get_property(libxml2_library TARGET ${imported_libs} PROPERTY LOCATION)` and passes the result
# unquoted to get_library_name(); an ALIAS target makes that a hard error under CMP0026 NEW, and an
# empty LOCATION breaks it too. This is also why we do not add_subdirectory() libxml2's own
# CMakeLists (which defines an ALIAS and unconditionally adds win32/libxml2.rc, which the cross
# toolchain cannot compile -- see cmake/WinMsvcCross.cmake's RC comment) and instead compile our
# own explicit source list into a real target above.
add_library(LibXml2::LibXml2 STATIC IMPORTED GLOBAL)
set(_llvm_ld_xml2_libfile "${_llvm_ld_xml2_out_dir}/${CMAKE_STATIC_LIBRARY_PREFIX}llvm_ld_xml2${CMAKE_STATIC_LIBRARY_SUFFIX}")
set(_llvm_ld_xml2_configs Release)
if(CMAKE_CONFIGURATION_TYPES)
  set(_llvm_ld_xml2_configs ${CMAKE_CONFIGURATION_TYPES})
  foreach(_llvm_ld_xml2_cfg ${_llvm_ld_xml2_configs})
    string(TOUPPER "${_llvm_ld_xml2_cfg}" _llvm_ld_xml2_cfg_upper)
    set_target_properties(llvm_ld_xml2 PROPERTIES
      ARCHIVE_OUTPUT_DIRECTORY_${_llvm_ld_xml2_cfg_upper} "${_llvm_ld_xml2_out_dir}")
  endforeach()
endif()
foreach(_llvm_ld_xml2_cfg ${_llvm_ld_xml2_configs})
  string(TOUPPER "${_llvm_ld_xml2_cfg}" _llvm_ld_xml2_cfg_upper)
  set_property(TARGET LibXml2::LibXml2 APPEND PROPERTY IMPORTED_CONFIGURATIONS "${_llvm_ld_xml2_cfg_upper}")
  set_target_properties(LibXml2::LibXml2 PROPERTIES
    IMPORTED_LOCATION_${_llvm_ld_xml2_cfg_upper} "${_llvm_ld_xml2_libfile}")
endforeach()
# Base (config-less) IMPORTED_LOCATION is what get_property(... PROPERTY LOCATION) falls back to
# outside a generator-expression context; keep it in sync with the per-config values above.
set_target_properties(LibXml2::LibXml2 PROPERTIES
  IMPORTED_LOCATION "${_llvm_ld_xml2_libfile}")

set_target_properties(LibXml2::LibXml2 PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "${LLVM_LD_LIBXML2_ROOT}/include;${_llvm_ld_xml2_gen_dir}"
  # Must also appear on consumers (WindowsManifestMerger.cpp), not just on llvm_ld_xml2's own
  # objects above: without it, xmlexports.h expands XMLPUBFUN to __declspec(dllimport) for
  # consuming translation units, which mismatches the plain (non-dllimport) symbols this static
  # archive actually exports and fails to link.
  INTERFACE_COMPILE_DEFINITIONS "LIBXML_STATIC")

if(WIN32)
  set_target_properties(LibXml2::LibXml2 PROPERTIES INTERFACE_LINK_LIBRARIES "bcrypt")
elseif(UNIX)
  set_target_properties(LibXml2::LibXml2 PROPERTIES INTERFACE_LINK_LIBRARIES "m")
endif()

# Guarded: the cross toolchain sets CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY (see
# cmake/WinMsvcCross.cmake), which restricts find_package's search to CMAKE_FIND_ROOT_PATH and can
# make an otherwise-harmless find_package(Threads) fail the whole cross configure. Windows needs no
# separate threading library (the CRT provides it), so skip the lookup entirely there.
if(NOT WIN32)
  find_package(Threads QUIET)
  if(TARGET Threads::Threads)
    set_property(TARGET LibXml2::LibXml2 APPEND PROPERTY INTERFACE_LINK_LIBRARIES "Threads::Threads")
  endif()
endif()

# Imported targets carry no build ordering of their own (CMake never schedules a build step for
# them), so this alone would not force llvm_ld_xml2 to be built before consumers of
# LibXml2::LibXml2. The root CMakeLists.txt adds the real ordering dependency
# (add_dependencies(LLVMWindowsManifest llvm_ld_xml2)) after add_subdirectory(llvm ...), once the
# LLVMWindowsManifest target actually exists.
