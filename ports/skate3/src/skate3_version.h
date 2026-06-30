#pragma once

// Hand-authored for the rexauto full-port (community CMake's configure_file
// version pipeline is not wired into the rexauto port CMakeLists). Mirrors the
// macros skate3_version.h.in would emit.

#ifndef SKATE3_BUILD_CONFIG
#define SKATE3_BUILD_CONFIG "release"
#endif

#define SKATE3_VERSION_STRING "1.0.1"
#define SKATE3_VERSION_NUMERIC "1.0.1"
#define SKATE3_BUILD_PLATFORM "win-amd64"
#define SKATE3_BUILD_TIMESTAMP "rexauto"

#define SKATE3_BUILD_TITLE "[v" SKATE3_VERSION_STRING "-" SKATE3_BUILD_CONFIG "]"
#define SKATE3_BUILD_STAMP \
  "build: skate3-v" SKATE3_VERSION_STRING "-" SKATE3_BUILD_PLATFORM \
  "-" SKATE3_BUILD_CONFIG "@" SKATE3_BUILD_TIMESTAMP
