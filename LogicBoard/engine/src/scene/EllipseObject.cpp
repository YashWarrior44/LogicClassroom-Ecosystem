// =============================================================================
// EllipseObject.cpp — Ellipse defined by center and radii (rx, ry).
// =============================================================================

#include "../../include/scene/EllipseObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

EllipseObject::EllipseObject()
    : WhiteboardObject() {}

EllipseObject::EllipseObject(UUID id)
    : WhiteboardObject(id) {}

EllipseObject::EllipseObject(Vec2f center, Vec2f radii)
    : WhiteboardObject(), center_(center), radii_(radii) {}

EllipseObject::EllipseObject(Vec2f center, Vec2f radii, UUID id)
    : WhiteboardObject(id), center_(center), radii_(radii) {}

// =============================================================================
// Factory helpers
// =============================================================================

EllipseObject EllipseObject::circle(Vec2f center, float radius) {
    return EllipseObject(center, {radius, radius});
}

EllipseObject EllipseObject::from_rect(Vec2f origin, Vec2f size) {
    return EllipseObject(
        {origin.x + size.x * 0.5f, origin.y + size.y * 0.5f},
        {size.x * 0.5f, size.y * 0.5f}
    );
}

// =============================================================================
// Bounding box
// =============================================================================

AABB EllipseObject::local_bounding_box() const {
    float half_w = stroke_width() * 0.5f;
    return AABB{
        {center_.x - radii_.x - half_w, center_.y - radii_.y - half_w},
        {center_.x + radii_.x + half_w, center_.y + radii_.y + half_w}
    };
}

// =============================================================================
// Hit-test
//
// The standard ellipse implicit equation is:
//   f(x,y) = ((x - cx) / rx)^2 + ((y - cy) / ry)^2
//
//   f <= 1  →  point is inside or on the ellipse
//   f >  1  →  point is outside
//
// For a filled ellipse: hit if f <= 1 (expanded by tolerance).
// For a stroke-only ellipse: hit if the point is close to the perimeter.
//
// Exact closest-point-on-ellipse is expensive (quartic solve), so we use
// a practical approximation:
//   1. Normalize the point into unit-circle space (divide by rx, ry).
//   2. In that space, the ellipse is a unit circle.
//   3. The closest point on the unit circle to the normalized point is
//      simply the normalized point scaled to unit length.
//   4. Map that back to ellipse space and compute the Euclidean distance.
//
// This approximation is exact for circles and very close for moderate
// eccentricities — more than sufficient for interactive hit-testing.
// =============================================================================

bool EllipseObject::hit_test_local(Vec2f p, float tolerance) const {
    // Avoid division by zero for degenerate ellipses.
    float rx = radii_.x;
    float ry = radii_.y;
    if (rx < 1e-6f && ry < 1e-6f) {
        return p.distance_sq(center_) <= tolerance * tolerance;
    }
    if (rx < 1e-6f) rx = 1e-6f;
    if (ry < 1e-6f) ry = 1e-6f;

    bool is_filled = fill_color().a > 0;

    // Offset from center.
    float dx = p.x - center_.x;
    float dy = p.y - center_.y;

    // Normalized coordinates (unit-circle space).
    float nx = dx / rx;
    float ny = dy / ry;

    float f = nx * nx + ny * ny;   // ellipse implicit value

    if (is_filled) {
        // Filled: check containment with tolerance expansion.
        // Expand radii by tolerance before testing.
        float erx = rx + tolerance;
        float ery = ry + tolerance;
        float enx = dx / erx;
        float eny = dy / ery;
        return (enx * enx + eny * eny) <= 1.0f;
    }

    // Stroke-only: approximate distance to perimeter.
    float half_sw = stroke_width() * 0.5f;
    float hit_r = half_sw + tolerance;

    if (f < 1e-12f) {
        // Point is at the center — distance to perimeter is min(rx, ry).
        float min_r = std::min(rx, ry);
        return min_r <= hit_r;
    }

    // Closest point on the ellipse (approximate):
    // In normalized space, project to unit circle, then scale back.
    float inv_len = 1.0f / std::sqrt(f);
    float closest_nx = nx * inv_len;
    float closest_ny = ny * inv_len;

    // Map back to ellipse space.
    float closest_x = closest_nx * rx;
    float closest_y = closest_ny * ry;

    // Distance in real coordinates.
    float ddx = dx - closest_x;
    float ddy = dy - closest_y;
    float dist_sq = ddx * ddx + ddy * ddy;

    return dist_sq <= hit_r * hit_r;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> EllipseObject::clone() const {
    auto copy = std::make_unique<EllipseObject>(center_, radii_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> EllipseObject::clone_exact() const {
    auto copy = std::make_unique<EllipseObject>(center_, radii_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void EllipseObject::serialize_properties(json& j) const {
    j["center"] = json::array({center_.x, center_.y});
    j["radii"]  = json::array({radii_.x, radii_.y});
}

void EllipseObject::deserialize_properties(const json& j) {
    if (j.contains("center") && j["center"].is_array() && j["center"].size() >= 2) {
        center_.x = j["center"][0].get<float>();
        center_.y = j["center"][1].get<float>();
    }
    if (j.contains("radii") && j["radii"].is_array() && j["radii"].size() >= 2) {
        radii_.x = j["radii"][0].get<float>();
        radii_.y = j["radii"][1].get<float>();
        if (radii_.x < 0.0f) radii_.x = 0.0f;
        if (radii_.y < 0.0f) radii_.y = 0.0f;
    }
}

} // namespace lb