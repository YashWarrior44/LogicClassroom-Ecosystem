#pragma once
// =============================================================================
// PptObject.h — An embedded PowerPoint presentation placed on the canvas.
//
// The engine stores a reference to the imported .pptx file (inside the board's
// "imports" folder), the destination rectangle, the total number of slides, the
// currently-shown slide index, and a per-slide animation/build step counter.
//
// The actual rendering (rasterizing slides to images) is the Python/Qt layer's
// responsibility. The engine only tracks state so that "advance animation" and
// "next slide" can be driven and persisted.
//
// JSON:
// {
//   "type": "ppt",
//   "src": "imports/lecture_3f9c.pptx",
//   "origin": [x, y],
//   "size":   [w, h],
//   "slide_count": 12,
//   "current_slide": 0,
//   "anim_step": 0,
//   "anim_counts": [3, 1, 5, ...],   // animation steps per slide (optional)
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <string>
#include <vector>
#include <cstdint>

namespace lb {

class PptObject : public WhiteboardObject {
public:
    PptObject();
    explicit PptObject(UUID id);
    PptObject(const std::string& src, Vec2f origin, Vec2f size);
    PptObject(const std::string& src, Vec2f origin, Vec2f size, UUID id);
    ~PptObject() override = default;

    // ---- Source reference ----
    const std::string& src() const { return src_; }
    void set_src(const std::string& s) { src_ = s; }

    // ---- Geometry ----
    Vec2f origin() const { return origin_; }
    void set_origin(Vec2f o) { origin_ = o; }

    Vec2f size() const { return size_; }
    void set_size(Vec2f s) {
        size_.x = (s.x < 0.0f) ? 0.0f : s.x;
        size_.y = (s.y < 0.0f) ? 0.0f : s.y;
    }

    float width() const { return size_.x; }
    float height() const { return size_.y; }

    Vec2f center() const { return origin_ + size_ * 0.5f; }

    // ---- Slide state ----
    int32_t slide_count() const { return slide_count_; }
    void set_slide_count(int32_t n) { slide_count_ = (n < 0) ? 0 : n; clamp_state(); }

    int32_t current_slide() const { return current_slide_; }
    void set_current_slide(int32_t i) { current_slide_ = i; clamp_state(); anim_step_ = 0; }

    // ---- Animation step (per current slide) ----
    int32_t anim_step() const { return anim_step_; }
    void set_anim_step(int32_t s) { anim_step_ = (s < 0) ? 0 : s; }

    /// Number of animation/build steps for the current slide (0 if unknown).
    int32_t anim_count_for_current() const {
        if (current_slide_ >= 0 &&
            static_cast<size_t>(current_slide_) < anim_counts_.size()) {
            return anim_counts_[current_slide_];
        }
        return 0;
    }

    void set_anim_counts(const std::vector<int32_t>& counts) { anim_counts_ = counts; }
    const std::vector<int32_t>& anim_counts() const { return anim_counts_; }

    /// Advance one animation step. If we're at the last animation step of the
    /// current slide, advance to the next slide (resetting anim_step to 0).
    /// Returns true if anything changed (false if already at the very end).
    bool advance_animation() {
        int32_t ac = anim_count_for_current();
        if (anim_step_ < ac) {
            anim_step_++;
            return true;
        }
        // Move to next slide if possible
        if (current_slide_ + 1 < slide_count_) {
            current_slide_++;
            anim_step_ = 0;
            return true;
        }
        return false;
    }

    /// Step backwards through animations / slides. Returns true if changed.
    bool retreat_animation() {
        if (anim_step_ > 0) {
            anim_step_--;
            return true;
        }
        if (current_slide_ > 0) {
            current_slide_--;
            anim_step_ = anim_count_for_current();
            return true;
        }
        return false;
    }

    void next_slide() {
        if (current_slide_ + 1 < slide_count_) { current_slide_++; anim_step_ = 0; }
    }
    void prev_slide() {
        if (current_slide_ > 0) { current_slide_--; anim_step_ = 0; }
    }

    // ---- WhiteboardObject interface ----
    AABB local_bounding_box() const override;
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "ppt"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    void clamp_state() {
        if (slide_count_ < 0) slide_count_ = 0;
        if (current_slide_ < 0) current_slide_ = 0;
        if (slide_count_ > 0 && current_slide_ >= slide_count_)
            current_slide_ = slide_count_ - 1;
        if (anim_step_ < 0) anim_step_ = 0;
    }

    std::string src_;
    Vec2f origin_;
    Vec2f size_;
    int32_t slide_count_   = 0;
    int32_t current_slide_ = 0;
    int32_t anim_step_     = 0;
    std::vector<int32_t> anim_counts_;
};

} // namespace lb
