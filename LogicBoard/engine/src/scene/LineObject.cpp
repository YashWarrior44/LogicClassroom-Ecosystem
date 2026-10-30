// =============================================================================
// LineObject.cpp — Straight line segment between two endpoints.
// =============================================================================

#include "../../include/scene/LineObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Construction
// =============================================================================

LineObject::LineObject()
    : WhiteboardObject() {}

LineObject::LineObject(UUID id)
    : WhiteboardObject(id) {}

LineObject::LineObject(Vec2f start, Vec2f end)
    : WhiteboardObject(), start_(start), end_(end) {}

LineObject::LineObject(Vec2f start, Vec2f end, UUID id)
    : WhiteboardObject(id), start_(start), end_(end) {}

// =============================================================================
// Bounding box
// =============================================================================

AABB LineObject::local_bounding_box() const {
    float half_w = stroke_width() * 0.5f;

    float xmin = std::min(start_.x, end_.x);
    float xmax = std::max(start_.x, end_.x);
    float ymin = std::min(start_.y, end_.y);
    float ymax = std::max(start_.y, end_.y);

    return AABB{
        {xmin - half_w, ymin - half_w},
        {xmax + half_w, ymax + half_w}
    };
}

// =============================================================================
// Hit-test: squared distance from point to line segment
// =============================================================================

bool LineObject::hit_test_local(Vec2f p, float tolerance) const {
    Vec2f ab = end_ - start_;
    float len_sq = ab.length_sq();

    float dist_sq;

    if (len_sq < 1e-12f) {
        // Degenerate segment (zero length) — distance to the single point.
        dist_sq = p.distance_sq(start_);
    } else {
        float t = (p - start_).dot(ab) / len_sq;
        t = std::max(0.0f, std::min(1.0f, t));
        Vec2f proj = start_ + ab * t;
        dist_sq = p.distance_sq(proj);
    }

    float hit_r = stroke_width() * 0.5f + tolerance;
    return dist_sq <= hit_r * hit_r;
}

// =============================================================================
// Clone
// =============================================================================

std::unique_ptr<WhiteboardObject> LineObject::clone() const {
    auto copy = std::make_unique<LineObject>(start_, end_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> LineObject::clone_exact() const {
    auto copy = std::make_unique<LineObject>(start_, end_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

// =============================================================================
// JSON
// =============================================================================

void LineObject::serialize_properties(json& j) const {
    j["start"] = json::array({start_.x, start_.y});
    j["end"]   = json::array({end_.x, end_.y});
}

void LineObject::deserialize_properties(const json& j) {
    if (j.contains("start") && j["start"].is_array() && j["start"].size() >= 2) {
        start_.x = j["start"][0].get<float>();
        start_.y = j["start"][1].get<float>();
    }
    if (j.contains("end") && j["end"].is_array() && j["end"].size() >= 2) {
        end_.x = j["end"][0].get<float>();
        end_.y = j["end"][1].get<float>();
    }
}

} // namespace lb