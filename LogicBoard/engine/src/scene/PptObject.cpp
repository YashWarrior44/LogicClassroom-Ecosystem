// =============================================================================
// PptObject.cpp — Embedded PowerPoint presentation with slide + animation state.
// =============================================================================

#include "../../include/scene/PptObject.h"
#include <nlohmann/json.hpp>
#include <algorithm>

using json = nlohmann::json;

namespace lb {

PptObject::PptObject()
    : WhiteboardObject() {}

PptObject::PptObject(UUID id)
    : WhiteboardObject(id) {}

PptObject::PptObject(const std::string& src, Vec2f origin, Vec2f size)
    : WhiteboardObject(), src_(src), origin_(origin), size_(size) {}

PptObject::PptObject(const std::string& src, Vec2f origin, Vec2f size, UUID id)
    : WhiteboardObject(id), src_(src), origin_(origin), size_(size) {}

AABB PptObject::local_bounding_box() const {
    return AABB{
        {origin_.x, origin_.y},
        {origin_.x + size_.x, origin_.y + size_.y}
    };
}

bool PptObject::hit_test_local(Vec2f p, float tolerance) const {
    float x0 = origin_.x - tolerance;
    float y0 = origin_.y - tolerance;
    float x1 = origin_.x + size_.x + tolerance;
    float y1 = origin_.y + size_.y + tolerance;
    return p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1;
}

std::unique_ptr<WhiteboardObject> PptObject::clone() const {
    auto copy = std::make_unique<PptObject>(src_, origin_, size_);
    copy->copy_base_from(*this);
    copy->set_id(UUID::generate());
    copy->slide_count_   = slide_count_;
    copy->current_slide_ = current_slide_;
    copy->anim_step_     = anim_step_;
    copy->anim_counts_   = anim_counts_;
    return copy;
}

std::unique_ptr<WhiteboardObject> PptObject::clone_exact() const {
    auto copy = std::make_unique<PptObject>(src_, origin_, size_, id());
    copy->copy_base_from(*this);
    copy->set_id(id());
    copy->slide_count_   = slide_count_;
    copy->current_slide_ = current_slide_;
    copy->anim_step_     = anim_step_;
    copy->anim_counts_   = anim_counts_;
    return copy;
}

void PptObject::serialize_properties(json& j) const {
    j["src"]           = src_;
    j["origin"]        = json::array({origin_.x, origin_.y});
    j["size"]          = json::array({size_.x, size_.y});
    j["slide_count"]   = slide_count_;
    j["current_slide"] = current_slide_;
    j["anim_step"]     = anim_step_;
    j["anim_counts"]   = anim_counts_;
}

void PptObject::deserialize_properties(const json& j) {
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
    if (j.contains("slide_count") && j["slide_count"].is_number_integer()) {
        slide_count_ = j["slide_count"].get<int32_t>();
    }
    if (j.contains("current_slide") && j["current_slide"].is_number_integer()) {
        current_slide_ = j["current_slide"].get<int32_t>();
    }
    if (j.contains("anim_step") && j["anim_step"].is_number_integer()) {
        anim_step_ = j["anim_step"].get<int32_t>();
    }
    if (j.contains("anim_counts") && j["anim_counts"].is_array()) {
        anim_counts_.clear();
        for (const auto& v : j["anim_counts"]) {
            if (v.is_number_integer()) anim_counts_.push_back(v.get<int32_t>());
        }
    }
    clamp_state();
}

} // namespace lb
