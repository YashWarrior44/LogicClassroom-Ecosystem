#pragma once
// =============================================================================
// CircleObject.h — A true circle defined by center and a single radius.
//
// Unlike EllipseObject (independent rx, ry), a circle has ONE degree of freedom
// for its size: the radius. Its scale() override forces uniform scaling so a
// non-uniform resize drag can never distort it into an ellipse.
//
// JSON:
// {
//   "type": "circle",
//   "center": [cx, cy],
//   "radius": r,
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"

namespace lb {

class CircleObject : public WhiteboardObject {
public:
    CircleObject();
    explicit CircleObject(UUID id);
    CircleObject(Vec2f center, float radius);
    CircleObject(Vec2f center, float radius, UUID id);
    ~CircleObject() override = default;

    // ---- Geometry ----

    Vec2f center() const { return center_; }
    void set_center(Vec2f c) { center_ = c; }

    float radius() const { return radius_; }
    void set_radius(float r) { radius_ = (r < 0.0f) ? 0.0f : r; }

    /// Build a circle inscribed in a (square-ish) bounding rectangle. Uses the
    /// smaller half-extent so the circle always fits inside the rect.
    static CircleObject from_rect(Vec2f origin, Vec2f size);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "circle"; }

    /// Force UNIFORM scaling so the circle never becomes an ellipse, regardless
    /// of how the caller drags resize handles. Uses the geometric mean of the
    /// two scale factors.
    void scale_uniform(float factor);

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f center_;
    float radius_ = 0.0f;
};

} // namespace lb