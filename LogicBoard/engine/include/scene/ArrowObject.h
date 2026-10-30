#pragma once
// =============================================================================
// ArrowObject.h — A line segment with an arrowhead at one or both ends.
//
// The arrowhead is a triangle defined by `head_length` (distance back from
// the tip along the shaft) and `head_width` (total spread perpendicular to
// the shaft).  The renderer draws the shaft as a line and the head(s) as
// filled triangles matching the stroke color.
//
// JSON:
// {
//   "type": "arrow",
//   "start": [x, y],
//   "end":   [x, y],
//   "head_length": 18.0,
//   "head_width":  12.0,
//   "head_at_start": false,
//   "head_at_end":   true,
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <array>

namespace lb {

class ArrowObject : public WhiteboardObject {
public:
    ArrowObject();
    explicit ArrowObject(UUID id);
    ArrowObject(Vec2f start, Vec2f end);
    ArrowObject(Vec2f start, Vec2f end, UUID id);
    ~ArrowObject() override = default;

    // ---- Geometry ----

    Vec2f start() const { return start_; }
    void set_start(Vec2f p) { start_ = p; }

    Vec2f end() const { return end_; }
    void set_end(Vec2f p) { end_ = p; }

    float length() const { return start_.distance(end_); }
    Vec2f direction() const { return (end_ - start_).normalized(); }
    Vec2f midpoint() const { return (start_ + end_) * 0.5f; }

    // ---- Arrowhead parameters ----

    /// Distance from the tip back along the shaft to the base of the head.
    float head_length() const { return head_length_; }
    void set_head_length(float v) { head_length_ = (v < 1.0f) ? 1.0f : v; }

    /// Total width of the arrowhead perpendicular to the shaft.
    float head_width() const { return head_width_; }
    void set_head_width(float v) { head_width_ = (v < 1.0f) ? 1.0f : v; }

    /// Whether to draw an arrowhead at the start endpoint.
    bool head_at_start() const { return head_at_start_; }
    void set_head_at_start(bool v) { head_at_start_ = v; }

    /// Whether to draw an arrowhead at the end endpoint (default: true).
    bool head_at_end() const { return head_at_end_; }
    void set_head_at_end(bool v) { head_at_end_ = v; }

    // ---- Computed arrowhead triangles ----

    /// The three vertices of the arrowhead triangle at the end point.
    /// Returns {{tip, left_wing, right_wing}}.  Only meaningful when
    /// head_at_end() is true and length() > 0.
    std::array<Vec2f, 3> end_head_triangle() const;

    /// The three vertices of the arrowhead triangle at the start point.
    std::array<Vec2f, 3> start_head_triangle() const;

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test against the shaft line and the arrowhead triangle(s).
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "arrow"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f start_;
    Vec2f end_;

    float head_length_   = 18.0f;
    float head_width_    = 12.0f;
    bool  head_at_start_ = false;
    bool  head_at_end_   = true;

    /// Helper: compute arrowhead triangle at `tip` pointing along `dir`.
    std::array<Vec2f, 3> compute_head(Vec2f tip, Vec2f dir) const;

    /// Helper: point-in-triangle test.
    static bool point_in_triangle(Vec2f p, Vec2f a, Vec2f b, Vec2f c);
};

} // namespace lb