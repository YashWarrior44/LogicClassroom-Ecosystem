// =============================================================================
// ImageObject.cpp — Raster image reference + destination rectangle.
// =============================================================================

#include "../../include/scene/ImageObject.h"
#include <nlohmann/json.hpp>
#include <algorithm>

using json = nlohmann::json;

namespace lb {

ImageObject::ImageObject()
    : WhiteboardObject() {}

ImageObject::ImageObject(UUID id)
    : WhiteboardObject(id) {}

ImageObject::ImageObject(const std::string& src, Vec2f origin, Vec2f size)
    : WhiteboardObject(), src_(src), origin_(origin), size_(size) {}

ImageObject::ImageObject(const std::string& src, Vec2f origin, Vec2f size, UUID id)
    : WhiteboardObject(id), src_(src), origin_(origin), size_(size) {}

AABB ImageObject::local_bounding_box() const {
    return AABB{
        {origin_.x, origin_.y},
        {origin_.x + size_.x, origin_.y + size_.y}
    };
}

bool ImageObject::hit_test_local(Vec2f p, float tolerance) const {
    // Images are solid rectangles — hit if inside (expanded by tolerance).
    float x0 = origin_.x - tolerance;
    float y0 = origin_.y - tolerance;
    float x1 = origin_.x + size_.x + tolerance;
    float y1 = origin_.y + size_.y + tolerance;
    return p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1;
}

std::unique_ptr<WhiteboardObject> ImageObject::clone() const {
    auto copy = std::make_unique<ImageObject>(src_, origin_, size_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->natural_size_ = natural_size_;
    return copy;
}

std::unique_ptr<WhiteboardObject> ImageObject::clone_exact() const {
    auto copy = std::make_unique<ImageObject>(src_, origin_, size_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->natural_size_ = natural_size_;
    return copy;
}

void ImageObject::serialize_properties(json& j) const {
    j["src"]          = src_;
    j["origin"]       = json::array({origin_.x, origin_.y});
    j["size"]         = json::array({size_.x, size_.y});
    j["natural_size"] = json::array({natural_size_.x, natural_size_.y});
}

void ImageObject::deserialize_properties(const json& j) {
    if (j.contains("src") && j["src"].is_string()) {
        src_ = j["src"].get<std::string>();
    }
    if (j.contains("origin") && j["origin"].is_array() && j["origin"].size() >= 2) {
        origin_.x = j["origin"][0].get<float>();
        origin_.y = j["origin"][1].get<float>();
    }
    if (j.contains("size") && j["size"].is_array() && j["size"].size() >= 2) {
        size_.x = std::max(0.0f, j["size"][0].get<float>());
        size_.y = std::max(0.0f, j["size"][1].get<float>());
    }
    if (j.contains("natural_size") && j["natural_size"].is_array() && j["natural_size"].size() >= 2) {
        natural_size_.x = j["natural_size"][0].get<float>();
        natural_size_.y = j["natural_size"][1].get<float>();
    }
}

} // namespace lb
