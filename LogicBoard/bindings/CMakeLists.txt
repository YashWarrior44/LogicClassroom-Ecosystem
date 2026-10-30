# =============================================================================
# bindings/CMakeLists.txt — Build the "logicboard_engine" Python extension.
# =============================================================================

file(GLOB BINDING_SOURCES "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")

pybind11_add_module(logicboard_engine ${BINDING_SOURCES})

target_link_libraries(logicboard_engine PRIVATE lb_engine)

# Ensure the module is importable as "logicboard_engine" with no prefix.
set_target_properties(logicboard_engine PROPERTIES
    PREFIX ""
    OUTPUT_NAME "logicboard_engine"
)

# Install the .so / .pyd next to the Python UI package.
install(TARGETS logicboard_engine
    LIBRARY DESTINATION "${CMAKE_SOURCE_DIR}/ui"
    RUNTIME DESTINATION "${CMAKE_SOURCE_DIR}/ui"
)