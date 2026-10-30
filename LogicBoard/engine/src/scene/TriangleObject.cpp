// =============================================================================
// TriangleObject.cpp — Three-vertex triangle with full engine integration:
// serialization, deserialization, hit-testing, bounding box, cloning.
// =============================================================================

#include "../../include/scene/TriangleObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>
#include <limits>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

TriangleObject::TriangleObject()
    : WhiteboardObject() {}

TriangleObject::TriangleObject(UUID id)
    : WhiteboardObject(id) {}

TriangleObject::TriangleObject(Vec2f p1, Vec2f p2, Vec2f p3)
    : WhiteboardObject(), p1_(p1), p2_(p2), p3_(p3) {}

TriangleObject::TriangleObject(Vec2f p1, Vec2f p2, Vec2f p3, UUID id)
    : WhiteboardObject(id), p1_(p1), p2_(p2), p3_(p3) {}

// =============================================================================
// Factory helper — drag-rectangle triangle (apex top-centre, base on bottom)
// =============================================================================

TriangleObject TriangleObject::from_drag_rect(Vec2f a, Vec2f b) {
    float x0 = std::min(a.x, b.x);
    float x1 = std::max(a.x, b.x);
    float y0 = std::min(a.y, b.y);
    float y1 = std::max(a.y, b.y);
    Vec2f apex { (x0 + x1) * 0.5f, y0 };
    Vec2f bl   { x0, y1 };
    Vec2f br   { x1, y1 };
    return TriangleObject(apex, bl, br);
}

// =============================================================================
// Derived geometry
// =============================================================================

float TriangleObject::signed_area() const {
    // Shoelace.
    return 0.5f * (
        p1_.x * (p2_.y - p3_.y) +
        p2_.x * (p3_.y - p1_.y) +
        p3_.x * (p1_.y - p2_.y)
    );
}

float TriangleObject::perimeter() const {
    return p1_.distance(p2_) + p2_.distance(p3_) + p3_.distance(p1_);
}

// =============================================================================
// Bounding box
// =============================================================================

AABB TriangleObject::local_bounding_box() const {
    float xmin = std::min(p1_.x, std::min(p2_.x, p3_.x));
    float ymin = std::min(p1_.y, std::min(p2_.y, p3_.y));
    float xmax = std::max(p1_.x, std::max(p2_.x, p3_.x));
    float ymax = std::max(p1_.y, std::max(p2_.y, p3_.y));
    // Inflate by half-stroke so the box always contains the rendered outline.
    float hw = stroke_width() * 0.5f;
    return AABB{
        {xmin - hw, ymin - hw},
        {xmax + hw, ymax + hw}
    };
}

// =============================================================================
// Hit-test helpers
// =============================================================================

namespace {

// Squared distance from point P to segment A–B.
float seg_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance_sq(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance_sq(proj);
}

// Sign of the half-plane test for triangle point-in-test.
float sign(Vec2f p, Vec2f a, Vec2f b) {
    return (p.x - b.x) * (a.y - b.y) - (a.x - b.x) * (p.y - b.y);
}

bool point_in_triangle(Vec2f p, Vec2f a, Vec2f b, Vec2f c) {
    float d1 = sign(p, a, b);
    float d2 = sign(p, b, c);
    float d3 = sign(p, c, a);
    bool has_neg = (d1 < 0) || (d2 < 0) || (d3 < 0);
    bool has_pos = (d1 > 0) || (d2 > 0) || (d3 > 0);
    return !(has_neg && has_pos);
}

} // namespace

bool TriangleObject::hit_test_local(Vec2f p, float tolerance) const {
    // Filled? Use point-in-triangle (with a tiny tolerance expansion that
    // also makes the edges hit-friendly).
    if (fill_color().a > 0) {
        // Cheap: try strict containment first.
        if (point_in_triangle(p, p1_, p2_, p3_)) return true;
        // Edge band: anywhere within (half_stroke + tolerance) of an edge.
        float r = stroke_width() * 0.5f + tolerance;
        float r2 = r * r;
        if (seg_dist_sq(p, p1_, p2_) <= r2) return true;
        if (seg_dist_sq(p, p2_, p3_) <= r2) return true;
        if (seg_dist_sq(p, p3_, p1_) <= r2) return true;
        return false;
    }

    // Stroke-only: distance to the closest edge within (half_stroke + tolerance).
    float r = stroke_width() * 0.5f + tolerance;
    float r2 = r * r;
    float best = std::numeric_limits<float>::max();
    best = std::min(best, seg_dist_sq(p, p1_, p2_));
    best = std::min(best, seg_dist_sq(p, p2_, p3_));
    best = std::min(best, seg_dist_sq(p, p3_, p1_));
    return best <= r2;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> TriangleObject::clone() const {
    auto copy = std::make_unique<TriangleObject>(p1_, p2_, p3_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> TriangleObject::clone_exact() const {
    auto copy = std::make_unique<TriangleObject>(p1_, p2_, p3_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void TriangleObject::serialize_properties(json& j) const {
    j["p1"] = json::array({p1_.x, p1_.y});
    j["p2"] = json::array({p2_.x, p2_.y});
    j["p3"] = json::array({p3_.x, p3_.y});
}

void TriangleObject::deserialize_properties(const json& j) {
    auto read_vec = [&](const char* key, Vec2f& out) {
        if (j.contains(key) && j[key].is_array() && j[key].size() >= 2) {
            out.x = j[key][0].get<float>();
            out.y = j[key][1].get<float>();
        }
    };
    read_vec("p1", p1_);
    read_vec("p2", p2_);
    read_vec("p3", p3_);

    // Forward-compat: accept a "vertices" array if a future writer chose that
    // shape (so old boards loaded by newer engines still round-trip cleanly).
    if (j.contains("vertices") && j["vertices"].is_array() && j["vertices"].size() >= 3) {
        const auto& v = j["vertices"];
        if (v[0].is_array() && v[0].size() >= 2) {
            p1_ = {v[0][0].get<float>(), v[0][1].get<float>()};
        }
        if (v[1].is_array() && v[1].size() >= 2) {
            p2_ = {v[1][0].get<float>(), v[1][1].get<float>()};
        }
        if (v[2].is_array() && v[2].size() >= 2) {
            p3_ = {v[2][0].get<float>(), v[2][1].get<float>()};
        }
    }
}

} // namespace lb
