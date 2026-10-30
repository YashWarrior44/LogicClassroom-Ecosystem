// =============================================================================
// ArrowObject.cpp — Line segment with arrowhead triangle(s) at one or both
// ends.  Arrowhead geometry is computed from head_length / head_width and
// the shaft direction vector.
// =============================================================================

#include "../../include/scene/ArrowObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

ArrowObject::ArrowObject()
    : WhiteboardObject() {}

ArrowObject::ArrowObject(UUID id)
    : WhiteboardObject(id) {}

ArrowObject::ArrowObject(Vec2f start, Vec2f end)
    : WhiteboardObject(), start_(start), end_(end) {}

ArrowObject::ArrowObject(Vec2f start, Vec2f end, UUID id)
    : WhiteboardObject(id), start_(start), end_(end) {}

// =============================================================================
// Arrowhead triangle computation
//
// Given a tip position and a direction vector pointing FROM the tip BACK
// along the shaft:
//
//         left_wing
//            /\
//           /  \
//    ------/tip \-----  (shaft continues behind)
//          \    /
//           \  /
//            \/
//         right_wing
//
// base_center = tip + dir * head_length
// left_wing   = base_center + perp * (head_width / 2)
// right_wing  = base_center - perp * (head_width / 2)
// =============================================================================

std::array<Vec2f, 3> ArrowObject::compute_head(Vec2f tip, Vec2f dir_away) const {
    Vec2f base_center = tip + dir_away * head_length_;
    Vec2f perp = dir_away.perp();
    float half_w = head_width_ * 0.5f;

    return {{
        tip,
        base_center + perp * half_w,
        base_center - perp * half_w
    }};
}

std::array<Vec2f, 3> ArrowObject::end_head_triangle() const {
    Vec2f dir = (start_ - end_).normalized();   // points away from tip (back along shaft)
    return compute_head(end_, dir);
}

std::array<Vec2f, 3> ArrowObject::start_head_triangle() const {
    Vec2f dir = (end_ - start_).normalized();   // points away from tip
    return compute_head(start_, dir);
}

// =============================================================================
// Point-in-triangle (barycentric coordinate method)
// =============================================================================

bool ArrowObject::point_in_triangle(Vec2f p, Vec2f a, Vec2f b, Vec2f c) {
    // Using sign of cross products to determine which side of each edge
    // the point lies on.  All three must agree for the point to be inside.
    auto sign = [](Vec2f p1, Vec2f p2, Vec2f p3) -> float {
        return (p1.x - p3.x) * (p2.y - p3.y)
             - (p2.x - p3.x) * (p1.y - p3.y);
    };

    float d1 = sign(p, a, b);
    float d2 = sign(p, b, c);
    float d3 = sign(p, c, a);

    bool has_neg = (d1 < 0.0f) || (d2 < 0.0f) || (d3 < 0.0f);
    bool has_pos = (d1 > 0.0f) || (d2 > 0.0f) || (d3 > 0.0f);

    return !(has_neg && has_pos);
}

// =============================================================================
// Bounding box — union of shaft segment and arrowhead triangle(s)
// =============================================================================

AABB ArrowObject::local_bounding_box() const {
    float half_w = stroke_width() * 0.5f;

    // Start with shaft segment.
    float xmin = std::min(start_.x, end_.x);
    float xmax = std::max(start_.x, end_.x);
    float ymin = std::min(start_.y, end_.y);
    float ymax = std::max(start_.y, end_.y);

    // Expand with arrowhead vertices.
    auto expand = [&](const std::array<Vec2f, 3>& tri) {
        for (const auto& v : tri) {
            xmin = std::min(xmin, v.x);
            xmax = std::max(xmax, v.x);
            ymin = std::min(ymin, v.y);
            ymax = std::max(ymax, v.y);
        }
    };

    if (head_at_end_)   expand(end_head_triangle());
    if (head_at_start_) expand(start_head_triangle());

    return AABB{
        {xmin - half_w, ymin - half_w},
        {xmax + half_w, ymax + half_w}
    };
}

// =============================================================================
// Hit-test: shaft segment distance OR point-in-arrowhead-triangle
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

// Minimum squared distance from point to any edge of a triangle.
float dist_sq_to_triangle_edges(Vec2f p, Vec2f a, Vec2f b, Vec2f c) {
    float d1 = seg_dist_sq(p, a, b);
    float d2 = seg_dist_sq(p, b, c);
    float d3 = seg_dist_sq(p, c, a);
    return std::min({d1, d2, d3});
}

} // anonymous namespace

bool ArrowObject::hit_test_local(Vec2f p, float tolerance) const {
    // Quick AABB rejection.
    AABB bb = local_bounding_box();
    if (!bb.expanded(tolerance).contains(p)) return false;

    float hit_r = stroke_width() * 0.5f + tolerance;
    float hit_r_sq = hit_r * hit_r;

    // Test shaft segment.
    if (seg_dist_sq(p, start_, end_) <= hit_r_sq) return true;

    // Test arrowhead at end.
    if (head_at_end_) {
        auto tri = end_head_triangle();
        if (point_in_triangle(p, tri[0], tri[1], tri[2])) return true;
        if (dist_sq_to_triangle_edges(p, tri[0], tri[1], tri[2]) <= hit_r_sq) return true;
    }

    // Test arrowhead at start.
    if (head_at_start_) {
        auto tri = start_head_triangle();
        if (point_in_triangle(p, tri[0], tri[1], tri[2])) return true;
        if (dist_sq_to_triangle_edges(p, tri[0], tri[1], tri[2]) <= hit_r_sq) return true;
    }

    return false;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> ArrowObject::clone() const {
    auto copy = std::make_unique<ArrowObject>(start_, end_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->head_length_   = head_length_;
    copy->head_width_    = head_width_;
    copy->head_at_start_ = head_at_start_;
    copy->head_at_end_   = head_at_end_;
    return copy;
}

std::unique_ptr<WhiteboardObject> ArrowObject::clone_exact() const {
    auto copy = std::make_unique<ArrowObject>(start_, end_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->head_length_   = head_length_;
    copy->head_width_    = head_width_;
    copy->head_at_start_ = head_at_start_;
    copy->head_at_end_   = head_at_end_;
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void ArrowObject::serialize_properties(json& j) const {
    j["start"]         = json::array({start_.x, start_.y});
    j["end"]           = json::array({end_.x, end_.y});
    j["head_length"]   = head_length_;
    j["head_width"]    = head_width_;
    j["head_at_start"] = head_at_start_;
    j["head_at_end"]   = head_at_end_;
}

void ArrowObject::deserialize_properties(const json& j) {
    if (j.contains("start") && j["start"].is_array() && j["start"].size() >= 2) {
        start_.x = j["start"][0].get<float>();
        start_.y = j["start"][1].get<float>();
    }
    if (j.contains("end") && j["end"].is_array() && j["end"].size() >= 2) {
        end_.x = j["end"][0].get<float>();
        end_.y = j["end"][1].get<float>();
    }
    if (j.contains("head_length") && j["head_length"].is_number()) {
        head_length_ = j["head_length"].get<float>();
        if (head_length_ < 1.0f) head_length_ = 1.0f;
    }
    if (j.contains("head_width") && j["head_width"].is_number()) {
        head_width_ = j["head_width"].get<float>();
        if (head_width_ < 1.0f) head_width_ = 1.0f;
    }
    if (j.contains("head_at_start") && j["head_at_start"].is_boolean()) {
        head_at_start_ = j["head_at_start"].get<bool>();
    }
    if (j.contains("head_at_end") && j["head_at_end"].is_boolean()) {
        head_at_end_ = j["head_at_end"].get<bool>();
    }
}

} // namespace lb