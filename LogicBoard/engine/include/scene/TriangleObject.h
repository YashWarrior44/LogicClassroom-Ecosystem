#pragma once
// =============================================================================
// TriangleObject.h — A triangle defined by three vertices in local space.
//
// A Triangle is its own first-class shape (not a 3-vertex PolygonObject) so the
// engine can give it dedicated rendering, hit-test, and bounding-box paths and
// so callers can `find_mut(id)` it back as a TriangleObject for tool behaviour
// that wants triangle-specific semantics.
//
// JSON:
// {
//   "type": "triangle",
//   "p1": [x, y],          // first vertex (apex by default for the shape tool)
//   "p2": [x, y],          // second vertex
//   "p3": [x, y],          // third vertex
//   ... base properties ...
// }
//
// The drawing tool creates a triangle from a drag rectangle as
//   p1 = apex (top-centre of the rect)
//   p2 = bottom-left
//   p3 = bottom-right
// but the class itself imposes no apex convention — any three points are valid.
// =============================================================================

#include "WhiteboardObject.h"

namespace lb {

class TriangleObject : public WhiteboardObject {
public:
    TriangleObject();
    explicit TriangleObject(UUID id);
    TriangleObject(Vec2f p1, Vec2f p2, Vec2f p3);
    TriangleObject(Vec2f p1, Vec2f p2, Vec2f p3, UUID id);
    ~TriangleObject() override = default;

    // ---- Vertices ----

    Vec2f p1() const { return p1_; }
    Vec2f p2() const { return p2_; }
    Vec2f p3() const { return p3_; }

    void set_p1(Vec2f v) { p1_ = v; }
    void set_p2(Vec2f v) { p2_ = v; }
    void set_p3(Vec2f v) { p3_ = v; }

    /// Set all three vertices at once.
    void set_vertices(Vec2f a, Vec2f b, Vec2f c) {
        p1_ = a; p2_ = b; p3_ = c;
    }

    /// Geometric centroid (average of the three vertices).
    Vec2f centroid() const {
        return Vec2f{
            (p1_.x + p2_.x + p3_.x) / 3.0f,
            (p1_.y + p2_.y + p3_.y) / 3.0f
        };
    }

    /// Signed area (positive = counter-clockwise winding).
    float signed_area() const;

    /// Total edge length.
    float perimeter() const;

    // ---- Factory helpers ----

    /// Build the shape-tool triangle (apex top-centre, base on the bottom)
    /// from a drag rectangle defined by two opposite corners.
    static TriangleObject from_drag_rect(Vec2f a, Vec2f b);

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// Hit-test: if filled (fill alpha > 0), point-in-triangle via barycentric
    /// coordinates. Otherwise, distance to the closest edge within the
    /// half-stroke + tolerance band.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "triangle"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    Vec2f p1_;
    Vec2f p2_;
    Vec2f p3_;
};

} // namespace lb
