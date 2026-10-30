#pragma once
// =============================================================================
// EllipseObject.h — An ellipse defined by center and radii (rx, ry).
// A circle is an ellipse with rx == ry.
//
// JSON:
// {
//   "type": "ellipse",
//   "center": [cx, cy],
//   "radii":  [rx, ry],
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"

namespace lb {

class EllipseObject : public WhiteboardObject {
public:
    EllipseObject();
    explicit EllipseObject(UUID id);
    EllipseObject(Vec2f center, Vec2f radii);
    EllipseObject(Vec2f center, Vec2f radii, UUID id);
    ~EllipseObject() override = default;

    // ---- Geometry ----

    Vec2f center() const { return center_; }
    void set_center(Vec2f c) { center_ = c; }

    /// Horizontal and vertical radii.
    Vec2f radii() const { return radii_; }
    void set_radii(Vec2f r) {
        radii_.x = (r.x < 0.0f) ? 0.0f : r.x;
        radii_.y = (r.y < 0.0f) ? 0.0f : r.y;
    }

    float rx() const { return radii_.x; }
    float ry() const { return radii_.y; }

    /// True when rx == ry (within floating-point tolerance).
    bool is_circle() const { return std::fabs(radii_.x - radii_.y) < 1e-4f; }

    /// Convenience: create a circle.
    static EllipseObject circle(Vec2f center, float radius);

    /// Build from a bounding rectangle (inscribed ellipse).
    static EllipseObject from_rect(Vec2f origin, Vec2f size);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test using the standard ellipse equation.  If unfilled, checks
    /// distance to the perimeter; if filled, checks containment.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "ellipse"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f center_;
    Vec2f radii_;
};

} // namespace lb