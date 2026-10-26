# Native vision-language model bridge (AI audio description, docs/plan/10).
# llama.cpp (MIT) is fetched at a pinned commit; only libmtmd + libllama
# are built and linked statically into libadpvlm.so.
cmake_minimum_required(VERSION 3.22)
project(adpvlm C CXX)

include(FetchContent)
FetchContent_Declare(llama
    URL https://github.com/ggml-org/llama.cpp/archive/7951f601e89494f2ed12dbd4eb52e254b8401197.tar.gz
    URL_HASH SHA256=8ba3b753c824b2938a693c0f8c4b00f4fb6e133c801df31d3cbd0a419aab1670
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)

set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
# A78/A55 (Dimensity 7300) and every arm64 phone since ~2019 have dotprod+fp16
set(GGML_CPU_ARM_ARCH "armv8.2-a+dotprod+fp16" CACHE STRING "" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
set(GGML_LLAMAFILE OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_MTMD ON CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(llama)

add_library(adpvlm SHARED vlm_jni.cpp)
target_link_libraries(adpvlm PRIVATE mtmd llama ggml android log)
target_link_options(adpvlm PRIVATE "-Wl,--gc-sections" "-Wl,-z,max-page-size=16384")
