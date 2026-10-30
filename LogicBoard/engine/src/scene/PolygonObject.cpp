// =============================================================================
// PolygonObject.cpp — Arbitrary polygon (closed) or polyline (open) defined
// by a list of vertices.
// =============================================================================

#include "../../include/scene/PolygonObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <numeric>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

PolygonObject::PolygonObject()
    : WhiteboardObject() {}

PolygonObject::PolygonObject(UUID id)
    : WhiteboardObject(id) {}

PolygonObject::PolygonObject(std::vector<Vec2f> vertices, bool closed)
    : WhiteboardObject(), vertices_(std::move(vertices)), closed_(closed) {}

PolygonObject::PolygonObject(std::vector<Vec2f> vertices, bool closed, UUID id)
    : WhiteboardObject(id), vertices_(std::move(vertices)), closed_(closed) {}

// =============================================================================
// Derived geometry
// =============================================================================

float PolygonObject::signed_area() const {
    size_t n = vertices_.size();
    if (n < 3) return 0.0f;

    // Shoelace formula.
    float area = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        size_t j = (i + 1) % n;
        area += vertices_[i].x * vertices_[j].y;
        area -= vertices_[j].x * vertices_[i].y;
    }
    return area * 0.5f;
}

Vec2f PolygonObject::centroid() const {
    size_t n = vertices_.size();
    if (n == 0) return {0.0f, 0.0f};
    if (n <= 2) {
        // Simple average for degenerate cases.
        Vec2f sum = {0.0f, 0.0f};
        for (const auto& v : vertices_) sum += v;
        return sum / static_cast<float>(n);
    }

    // Geometric centroid using the shoelace-based formula.
    float a = signed_area();
    if (std::fabs(a) < 1e-10f) {
        // Degenerate polygon — fallback to vertex average.
        Vec2f sum = {0.0f, 0.0f};
        for (const auto& v : vertices_) sum += v;
        return sum / static_cast<float>(n);
    }

    float cx = 0.0f, cy = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        size_t j = (i + 1) % n;
        float cross = vertices_[i].x * vertices_[j].y
                    - vertices_[j].x * vertices_[i].y;
        cx += (vertices_[i].x + vertices_[j].x) * cross;
        cy += (vertices_[i].y + vertices_[j].y) * cross;
    }

    float inv_6a = 1.0f / (6.0f * a);
    return {cx * inv_6a, cy * inv_6a};
}

float PolygonObject::perimeter() const {
    size_t n = vertices_.size();
    if (n < 2) return 0.0f;

    float total = 0.0f;
    size_t limit = closed_ ? n : n - 1;
    for (size_t i = 0; i < limit; ++i) {
        size_t j = (i + 1) % n;
        total += vertices_[i].distance(vertices_[j]);
    }
    return total;
}

// =============================================================================
// Bounding box
// =============================================================================

AABB PolygonObject::local_bounding_box() const {
    if (vertices_.empty()) return AABB();

    AABB bb = AABB::from_points(vertices_.data(), vertices_.size());
    float half_w = stroke_width() * 0.5f;
    return bb.expanded(half_w);
}

// =============================================================================
// Point-in-polygon — ray casting (even-odd rule)
//
// Cast a horizontal ray from point P to +X infinity and count how many
// polygon edges it crosses.  Odd count = inside.
// =============================================================================

bool PolygonObject::point_in_polygon(Vec2f p) const {
    size_t n = vertices_.size();
    if (n < 3) return false;

    bool inside = false;
    for (size_t i = 0, j = n - 1; i < n; j = i++) {
        float yi = vertices_[i].y;
        float yj = vertices_[j].y;

        // Does this edge straddle the horizontal ray at p.y?
        if ((yi > p.y) != (yj > p.y)) {
            // X coordinate of intersection.
            float x_intersect = vertices_[i].x
                + (p.y - yi) / (yj - yi) * (vertices_[j].x - vertices_[i].x);
            if (p.x < x_intersect) {
                inside = !inside;
            }
        }
    }
    return inside;
}

// =============================================================================
// Minimum distance to edges
// =============================================================================

namespace {

float seg_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance_sq(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance_sq(proj);
}

} // anonymous namespace

float PolygonObject::distance_to_edges(Vec2f p) const {
    size_t n = vertices_.size();
    if (n == 0) return std::numeric_limits<float>::max();
    if (n == 1) return p.distance(vertices_[0]);

    float best_sq = std::numeric_limits<float>::max();
    size_t limit = closed_ ? n : n - 1;
    for (size_t i = 0; i < limit; ++i) {
        size_t j = (i + 1) % n;
        float d_sq = seg_dist_sq(p, vertices_[i], vertices_[j]);
        best_sq = std::min(best_sq, d_sq);
    }
    return std::sqrt(best_sq);
}

// =============================================================================
// Hit-test
//
// Closed + filled: point-in-polygon (expanded by tolerance).
// Closed + unfilled or open: distance to nearest edge <= hit radius.
// =============================================================================

bool PolygonObject::hit_test_local(Vec2f local_point, float tolerance) const {
    if (vertices_.empty()) return false;

    // Quick AABB rejection.
    AABB bb = local_bounding_box();
    if (!bb.expanded(tolerance).contains(local_point)) return false;

    bool is_filled = closed_ && fill_color().a > 0;

    if (is_filled) {
        // Check containment first (no tolerance expansion needed for interior).
        if (point_in_polygon(local_point)) return true;

        // Also hit if within tolerance of any edge (for clicking near borders).
        float hit_r = stroke_width() * 0.5f + tolerance;
        float dist = distance_to_edges(local_point);
        return dist <= hit_r;
    }

    // Stroke-only: distance to nearest edge.
    float hit_r = stroke_width() * 0.5f + tolerance;
    float dist = distance_to_edges(local_point);
    return dist <= hit_r;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> PolygonObject::clone() const {
    auto copy = std::make_unique<PolygonObject>(vertices_, closed_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> PolygonObject::clone_exact() const {
    auto copy = std::make_unique<PolygonObject>(vertices_, closed_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void PolygonObject::serialize_properties(json& j) const {
    json verts = json::array();
    verts.get_ref<json::array_t&>().reserve(vertices_.size());
    for (const auto& v : vertices_) {
        verts.push_back(json::array({v.x, v.y}));
    }
    j["vertices"] = std::move(verts);
    j["closed"]   = closed_;
}

void PolygonObject::deserialize_properties(const json& j) {
    vertices_.clear();

    if (j.contains("vertices") && j["vertices"].is_array()) {
        const auto& arr = j["vertices"];
        vertices_.reserve(arr.size());
        for (const auto& v : arr) {
            if (v.is_array() && v.size() >= 2) {
                vertices_.push_back({v[0].get<float>(), v[1].get<float>()});
            }
        }
    }

    if (j.contains("closed") && j["closed"].is_boolean()) {
        closed_ = j["closed"].get<bool>();
    }
}

} // namespace lb