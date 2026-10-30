#pragma once
// =============================================================================
// RectObject.h — A rectangle defined by origin (top-left) and size, with
// optional rounded corners.
//
// JSON:
// {
//   "type": "rect",
//   "origin": [x, y],
//   "size":   [w, h],
//   "corner_radius": 0.0,
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"

namespace lb {

class RectObject : public WhiteboardObject {
public:
    RectObject();
    explicit RectObject(UUID id);
    RectObject(Vec2f origin, Vec2f size);
    RectObject(Vec2f origin, Vec2f size, UUID id);
    ~RectObject() override = default;

    // ---- Geometry ----

    /// Top-left corner in local coordinates.
    Vec2f origin() const { return origin_; }
    void set_origin(Vec2f o) { origin_ = o; }

    /// Width and height.
    Vec2f size() const { return size_; }
    void set_size(Vec2f s) {
        size_.x = (s.x < 0.0f) ? 0.0f : s.x;
        size_.y = (s.y < 0.0f) ? 0.0f : s.y;
    }

    float width() const { return size_.x; }
    float height() const { return size_.y; }

    /// Uniform corner radius (0 = sharp corners).
    float corner_radius() const { return corner_radius_; }
    void set_corner_radius(float r) { corner_radius_ = (r < 0.0f) ? 0.0f : r; }

    /// Center of the rectangle.
    Vec2f center() const { return origin_ + size_ * 0.5f; }

    /// Build from center + half-size.
    static RectObject from_center(Vec2f center, Vec2f half_size);

    /// Build from two arbitrary corner points (normalizes to top-left + size).
    static RectObject from_corners(Vec2f a, Vec2f b);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test: if fill is transparent, tests against the border only
    /// (within `tolerance`).  If filled, tests whether the point is inside
    /// the rectangle.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "rect"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f origin_;
    Vec2f size_;
    float corner_radius_ = 0.0f;
};

} // namespace lb