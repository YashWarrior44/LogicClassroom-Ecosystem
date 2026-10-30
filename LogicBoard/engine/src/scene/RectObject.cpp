// =============================================================================
// RectObject.cpp — Axis-aligned rectangle with optional corner radius.
// =============================================================================

#include "../../include/scene/RectObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

RectObject::RectObject()
    : WhiteboardObject() {}

RectObject::RectObject(UUID id)
    : WhiteboardObject(id) {}

RectObject::RectObject(Vec2f origin, Vec2f size)
    : WhiteboardObject(), origin_(origin), size_(size) {}

RectObject::RectObject(Vec2f origin, Vec2f size, UUID id)
    : WhiteboardObject(id), origin_(origin), size_(size) {}

// =============================================================================
// Factory helpers
// =============================================================================

RectObject RectObject::from_center(Vec2f center, Vec2f half_size) {
    return RectObject(center - half_size, half_size * 2.0f);
}

RectObject RectObject::from_corners(Vec2f a, Vec2f b) {
    float xmin = std::min(a.x, b.x);
    float ymin = std::min(a.y, b.y);
    float xmax = std::max(a.x, b.x);
    float ymax = std::max(a.y, b.y);
    return RectObject({xmin, ymin}, {xmax - xmin, ymax - ymin});
}

// =============================================================================
// Bounding box
// =============================================================================

AABB RectObject::local_bounding_box() const {
    float half_w = stroke_width() * 0.5f;
    return AABB{
        {origin_.x - half_w, origin_.y - half_w},
        {origin_.x + size_.x + half_w, origin_.y + size_.y + half_w}
    };
}

// =============================================================================
// Hit-test
//
// Two modes:
//   1. Filled (fill_color alpha > 0): point inside the rectangle.
//   2. Stroke-only: point within `tolerance + half_stroke` of any edge.
//
// Corner radius is handled by checking the four corner circle regions:
// if the point is in a corner quadrant beyond the radius inset, the
// distance to the corner arc determines inside/outside.
// =============================================================================

namespace {

// Check if a point is inside a rounded rectangle (solid fill).
bool point_in_rounded_rect(Vec2f p, Vec2f origin, Vec2f size, float cr) {
    float x0 = origin.x, y0 = origin.y;
    float x1 = origin.x + size.x, y1 = origin.y + size.y;

    // Outside the outer bounds?
    if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) return false;

    // If no corner radius, the AABB check is sufficient.
    if (cr <= 0.0f) return true;

    // Clamp corner radius to half the smaller dimension.
    cr = std::min(cr, std::min(size.x * 0.5f, size.y * 0.5f));

    // Check the four corner quadrants.
    // Top-left corner
    if (p.x < x0 + cr && p.y < y0 + cr) {
        Vec2f center = {x0 + cr, y0 + cr};
        return p.distance_sq(center) <= cr * cr;
    }
    // Top-right corner
    if (p.x > x1 - cr && p.y < y0 + cr) {
        Vec2f center = {x1 - cr, y0 + cr};
        return p.distance_sq(center) <= cr * cr;
    }
    // Bottom-left corner
    if (p.x < x0 + cr && p.y > y1 - cr) {
        Vec2f center = {x0 + cr, y1 - cr};
        return p.distance_sq(center) <= cr * cr;
    }
    // Bottom-right corner
    if (p.x > x1 - cr && p.y > y1 - cr) {
        Vec2f center = {x1 - cr, y1 - cr};
        return p.distance_sq(center) <= cr * cr;
    }

    return true;
}

// Minimum distance from point P to line segment A–B (squared).
float seg_dist_sq(Vec2f p, Vec2f a, Vec2f b) {
    Vec2f ab = b - a;
    float len_sq = ab.length_sq();
    if (len_sq < 1e-12f) return p.distance_sq(a);
    float t = (p - a).dot(ab) / len_sq;
    t = std::max(0.0f, std::min(1.0f, t));
    Vec2f proj = a + ab * t;
    return p.distance_sq(proj);
}

// Minimum distance from point to the perimeter of an axis-aligned rectangle
// with corner radius.  Returns squared distance.
float dist_sq_to_rect_perimeter(Vec2f p, Vec2f origin, Vec2f size, float cr) {
    float x0 = origin.x, y0 = origin.y;
    float x1 = origin.x + size.x, y1 = origin.y + size.y;

    cr = std::min(cr, std::min(size.x * 0.5f, size.y * 0.5f));
    if (cr < 0.0f) cr = 0.0f;

    // The perimeter consists of 4 straight edges (inset by cr at the corners)
    // and 4 quarter-circle arcs.

    float best = std::numeric_limits<float>::max();

    // Top edge (from x0+cr to x1-cr at y0)
    best = std::min(best, seg_dist_sq(p, {x0 + cr, y0}, {x1 - cr, y0}));
    // Bottom edge
    best = std::min(best, seg_dist_sq(p, {x0 + cr, y1}, {x1 - cr, y1}));
    // Left edge
    best = std::min(best, seg_dist_sq(p, {x0, y0 + cr}, {x0, y1 - cr}));
    // Right edge
    best = std::min(best, seg_dist_sq(p, {x1, y0 + cr}, {x1, y1 - cr}));

    if (cr > 0.0f) {
        // Four corner arcs: distance to the arc = |dist_to_center - cr|.
        Vec2f corners[4] = {
            {x0 + cr, y0 + cr},   // top-left
            {x1 - cr, y0 + cr},   // top-right
            {x0 + cr, y1 - cr},   // bottom-left
            {x1 - cr, y1 - cr},   // bottom-right
        };

        for (const auto& c : corners) {
            float d = p.distance(c);
            float arc_dist = std::fabs(d - cr);
            best = std::min(best, arc_dist * arc_dist);
        }
    }

    return best;
}

} // anonymous namespace

bool RectObject::hit_test_local(Vec2f local_point, float tolerance) const {
    bool is_filled = fill_color().a > 0;

    if (is_filled) {
        // Filled: check containment (expanded by tolerance).
        Vec2f expanded_origin = {origin_.x - tolerance, origin_.y - tolerance};
        Vec2f expanded_size   = {size_.x + 2.0f * tolerance, size_.y + 2.0f * tolerance};
        return point_in_rounded_rect(local_point, expanded_origin, expanded_size, corner_radius_ + tolerance);
    }

    // Stroke-only: check distance to the perimeter.
    float hit_r = stroke_width() * 0.5f + tolerance;
    float dist_sq = dist_sq_to_rect_perimeter(local_point, origin_, size_, corner_radius_);
    return dist_sq <= hit_r * hit_r;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> RectObject::clone() const {
    auto copy = std::make_unique<RectObject>(origin_, size_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->corner_radius_ = corner_radius_;
    return copy;
}

std::unique_ptr<WhiteboardObject> RectObject::clone_exact() const {
    auto copy = std::make_unique<RectObject>(origin_, size_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->corner_radius_ = corner_radius_;
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void RectObject::serialize_properties(json& j) const {
    j["origin"]        = json::array({origin_.x, origin_.y});
    j["size"]          = json::array({size_.x, size_.y});
    j["corner_radius"] = corner_radius_;
}

void RectObject::deserialize_properties(const json& j) {
    if (j.contains("origin") && j["origin"].is_array() && j["origin"].size() >= 2) {
        origin_.x = j["origin"][0].get<float>();
        origin_.y = j["origin"][1].get<float>();
    }
    if (j.contains("size") && j["size"].is_array() && j["size"].size() >= 2) {
        size_.x = j["size"][0].get<float>();
        size_.y = j["size"][1].get<float>();
        if (size_.x < 0.0f) size_.x = 0.0f;
        if (size_.y < 0.0f) size_.y = 0.0f;
    }
    if (j.contains("corner_radius") && j["corner_radius"].is_number()) {
        corner_radius_ = j["corner_radius"].get<float>();
        if (corner_radius_ < 0.0f) corner_radius_ = 0.0f;
    }
}

} // namespace lb