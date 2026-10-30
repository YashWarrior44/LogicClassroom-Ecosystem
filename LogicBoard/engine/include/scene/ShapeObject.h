#pragma once
// =============================================================================
// ShapeObject.h — every shape tool that is not one of the original six.
//
// Parallelograms, stars, dashed lines, cubes, cones... are all ONE object
// type: a kind name plus the two points of the drag that drew it. What a
// "cone" looks like is decided in one place on the Python side
// (ui/canvas/shape_geometry.py), which both renders it and draws its icon —
// the engine only needs to know where the shape is: its box for selection
// and hit-testing, and how to save and load it.
//
//   * LINE kinds ("dashed_line", "dotted_line", "double_arrow"...) run from
//     start to end; hit-testing is distance to that segment.
//   * Every other kind fills the box start and end span; hit-testing is the
//     box, which is what a hand on a touch board expects of a cube.
//
// Robustness, because a malformed board must never take the app down:
//   * the kind is capped at kMaxKindLength characters;
//   * every coordinate read from JSON or set from Python is checked to be a
//     finite number, and replaced by 0 if it is not — NaN in a bounding box
//     poisons every spatial query that touches it;
//   * nothing divides by a length that can be zero.
//
// JSON:
// {
//   "type": "shape",
//   "kind": "cone",
//   "start": [x, y],
//   "end":   [x, y],
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <string>

namespace lb {

class ShapeObject : public WhiteboardObject {
public:
    static constexpr size_t kMaxKindLength = 64;

    ShapeObject();
    explicit ShapeObject(UUID id);
    ShapeObject(std::string kind, Vec2f start, Vec2f end);
    ShapeObject(std::string kind, Vec2f start, Vec2f end, UUID id);
    ~ShapeObject() override = default;

    const std::string& kind() const { return kind_; }
    void set_kind(const std::string& k);

    Vec2f start() const { return start_; }
    Vec2f end() const { return end_; }
    void set_start(Vec2f v);
    void set_end(Vec2f v);

    /// True for the kinds drawn from start to end rather than in a box.
    bool is_line_kind() const;

    // ---- WhiteboardObject interface ----
    AABB local_bounding_box() const override;
    bool hit_test_local(Vec2f local_point, float tolerance) const override;
    const char* type_name() const override { return "shape"; }
    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    std::string kind_;
    Vec2f start_;
    Vec2f end_;
};

} // namespace lb
