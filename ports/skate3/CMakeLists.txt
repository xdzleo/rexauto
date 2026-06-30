# skate3 - ReXGlue Recompiled Project
#
# SDK boilerplate lives in generated/rexglue.cmake.

cmake_minimum_required(VERSION 3.25)
project(skate3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(generated/rexglue.cmake)

# Sources
#
# rexauto full-port: adopt the proven community Skate3BaseApp app sources
# (mirrors the community CMakeLists' SKATE3_COMMON_SOURCES) so the retail boot
# path matches the PROVEN-playable configuration. TU-only sources
# (skate3_title_update_installer.cpp) and exception_compat.cpp are intentionally
# omitted: this is the retail (non-TU) build (SKATE3_HAS_TITLE_UPDATE=0) and the
# retail exception-guard symbol sub_82F44E40 is not present in this port's
# generated set, so exception_compat.cpp would not link.
set(SKATE3_SOURCES
    src/main.cpp
    src/skate3_app_common.cpp
    src/skate3_fov.cpp
    src/skate3_iso_installer.cpp
    src/skate3_user_settings.cpp
    src/skate3_exception_compat.cpp
)

if(WIN32)
    add_executable(skate3 WIN32 ${SKATE3_SOURCES})
else()
    add_executable(skate3 ${SKATE3_SOURCES})
endif()

rexglue_setup_target(skate3)

# Retail / non-TU build: the title update code path stays disabled (the game
# data is already installed; this is the proven-playable retail configuration).
target_compile_definitions(skate3 PRIVATE
    SKATE3_HAS_TITLE_UPDATE=0
    SKATE3_BUILD_CONFIG="$<CONFIG>"
)

# The community app sources include "generated/default/skate3_init.h",
# "skate3_app_common.h" and <skate3_version.h>; src/ + the port root are already
# on the include path via rexglue_setup_target, but make it explicit.
target_include_directories(skate3 PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

if(WIN32)
    target_link_libraries(skate3 PRIVATE comdlg32 winhttp)
endif()

# rexauto: extra recompiled modules (EAWebkit etc.) linked into the same exe.
# rexglue_setup_target already consumed the entrypoint's generated sources, so
# reusing GENERATED_SOURCES here is safe (save/restore keeps it tidy). The
# target's compile options (incl. /EHa) already apply to these added sources.
foreach(_rexauto_mod eawebkit)
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}/sources.cmake")
        set(_rexauto_saved "${GENERATED_SOURCES}")
        include(generated/${_rexauto_mod}/sources.cmake)
        target_sources(skate3 PRIVATE ${GENERATED_SOURCES})
        target_include_directories(skate3 PRIVATE
            "${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}")
        set(GENERATED_SOURCES "${_rexauto_saved}")
        unset(_rexauto_saved)
    endif()
endforeach()
