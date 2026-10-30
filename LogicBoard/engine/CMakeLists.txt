# Engine CMake
file(GLOB_RECURSE ENGINE_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")

add_library(lb_engine STATIC ${ENGINE_SOURCES})

target_include_directories(lb_engine PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include"
)

target_link_libraries(lb_engine PUBLIC nlohmann_json::nlohmann_json)