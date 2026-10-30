// =============================================================================
// CircleObject.cpp — A true circle (center + single radius).
// =============================================================================

#include "../../include/scene/CircleObject.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cmath>

using json = nlohmann::json;

namespace lb {

// ---- Construction ----------------------------------------------------------

CircleObject::CircleObject()
    : WhiteboardObject() {}

CircleObject::CircleObject(UUID id)
    : WhiteboardObject(id) {}

CircleObject::CircleObject(Vec2f center, float radius)
    : WhiteboardObject(), center_(center), radius_(radius < 0.0f ? 0.0f : radius) {}

CircleObject::CircleObject(Vec2f center, float radius, UUID id)
    : WhiteboardObject(id), center_(center), radius_(radius < 0.0f ? 0.0f : radius) {}

CircleObject CircleObject::from_rect(Vec2f origin, Vec2f size) {
    float r = std::min(std::fabs(size.x), std::fabs(size.y)) * 0.5f;
    return CircleObject({origin.x + size.x * 0.5f, origin.y + size.y * 0.5f}, r);
}

// ---- Bounding box ----------------------------------------------------------

AABB CircleObject::local_bounding_box() const {
    float half_w = stroke_width() * 0.5f;
    float r = radius_ + half_w;
    return AABB{
        {center_.x - r, center_.y - r},
        {center_.x + r, center_.y + r}
    };
}

// ---- Hit-test --------------------------------------------------------------
// Filled: inside if distance-to-center <= radius (+tolerance).
// Stroke-only: hit if distance-to-center is near the perimeter band.

bool CircleObject::hit_test_local(Vec2f p, float tolerance) const {
    float dx = p.x - center_.x;
    float dy = p.y - center_.y;
    float dist = std::sqrt(dx * dx + dy * dy);

    bool is_filled = fill_color().a > 0;
    if (is_filled) {
        return dist <= radius_ + tolerance;
    }
    float half_sw = stroke_width() * 0.5f;
    float hit_r = half_sw + tolerance;
    return std::fabs(dist - radius_) <= hit_r;
}

// ---- Uniform scale ---------------------------------------------------------

void CircleObject::scale_uniform(float factor) {
    if (factor < 0.0f) factor = 0.0f;
    Vec2f c = local_bounding_box().center();
    AffineTransform s = AffineTransform::translation(c.x, c.y)
                            .then(AffineTransform::scaling(factor, factor))
                            .then(AffineTransform::translation(-c.x, -c.y));
    set_transform(s.then(transform()));
}

// ---- Clone -----------------------------------------------------------------

std::unique_ptr<WhiteboardObject> CircleObject::clone() const {
    auto copy = std::make_unique<CircleObject>(center_, radius_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    return copy;
}

std::unique_ptr<WhiteboardObject> CircleObject::clone_exact() const {
    auto copy = std::make_unique<CircleObject>(center_, radius_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    return copy;
}

// ---- JSON ------------------------------------------------------------------

void CircleObject::serialize_properties(json& j) const {
    j["center"] = json::array({center_.x, center_.y});
    j["radius"] = radius_;
}

void CircleObject::deserialize_properties(const json& j) {
    if (j.contains("center") && j["center"].is_array() && j["center"].size() >= 2) {
        center_.x = j["center"][0].get<float>();
        center_.y = j["center"][1].get<float>();
    }
    if (j.contains("radius") && j["radius"].is_number()) {
        radius_ = j["radius"].get<float>();
        if (radius_ < 0.0f) radius_ = 0.0f;
    }
}

} // namespace lb