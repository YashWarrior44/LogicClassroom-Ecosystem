#pragma once
// =============================================================================
// PolygonObject.h — An arbitrary polygon or polyline defined by a list of
// vertices.  Can be open (polyline) or closed (polygon).
//
// When closed and filled, the interior is rendered.  When open, only the
// stroke path is drawn.
//
// JSON:
// {
//   "type": "polygon",
//   "vertices": [[x0,y0], [x1,y1], ...],
//   "closed": true,
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <vector>

namespace lb {

class PolygonObject : public WhiteboardObject {
public:
    PolygonObject();
    explicit PolygonObject(UUID id);
    explicit PolygonObject(std::vector<Vec2f> vertices, bool closed = true);
    PolygonObject(std::vector<Vec2f> vertices, bool closed, UUID id);
    ~PolygonObject() override = default;

    // ---- Vertices ----

    const std::vector<Vec2f>& vertices() const { return vertices_; }
    size_t vertex_count() const { return vertices_.size(); }
    bool empty() const { return vertices_.empty(); }

    void set_vertices(std::vector<Vec2f> verts) { vertices_ = std::move(verts); }

    /// Access a single vertex by index.
    Vec2f vertex(size_t i) const { return vertices_.at(i); }
    void set_vertex(size_t i, Vec2f v) { vertices_.at(i) = v; }

    void add_vertex(Vec2f v) { vertices_.push_back(v); }
    void insert_vertex(size_t i, Vec2f v) { vertices_.insert(vertices_.begin() + static_cast<ptrdiff_t>(i), v); }
    void remove_vertex(size_t i) { vertices_.erase(vertices_.begin() + static_cast<ptrdiff_t>(i)); }

    // ---- Open / closed ----

    bool closed() const { return closed_; }
    void set_closed(bool c) { closed_ = c; }

    // ---- Derived geometry ----

    /// Compute the signed area (positive = counter-clockwise winding).
    /// Only meaningful when closed.
    float signed_area() const;

    /// Centroid of the polygon (average of vertices, or geometric centroid
    /// for closed polygons).
    Vec2f centroid() const;

    /// Total perimeter length.
    float perimeter() const;

    // ---- WhiteboardObject interface ----

    AABB local_bounding_box() const override;

    /// If closed + filled: point-in-polygon (ray-cast).
    /// If closed + unfilled or open: distance-to-edge check.
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "polygon"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    std::vector<Vec2f> vertices_;
    bool               closed_ = true;

    /// Point-in-polygon via ray casting (even-odd rule).
    bool point_in_polygon(Vec2f p) const;

    /// Minimum distance from point to any edge of the polygon/polyline.
    float distance_to_edges(Vec2f p) const;
};

} // namespace lb