cmake_minimum_required(VERSION 3.18)
project(LogicBoard VERSION 3.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Disable nlohmann/json ABI versioning inline namespace
# so 'nlohmann::json' is unambiguous everywhere
add_compile_definitions(NLOHMANN_JSON_NAMESPACE_NO_VERSION=1)

include(FetchContent)

# nlohmann/json (header-only)
FetchContent_Declare(json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(json)

# pybind11
FetchContent_Declare(pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v2.12.0
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(pybind11)

add_subdirectory(engine)
add_subdirectory(bindings)
