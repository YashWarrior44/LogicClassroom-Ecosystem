#pragma once
// =============================================================================
// ImageObject.h — A raster image placed on the canvas.
//
// The engine does NOT decode or store pixel data; it stores a reference to a
// file inside the board's "imports" folder plus the destination rectangle.
// The Python/Qt layer is responsible for loading and drawing the actual bitmap.
//
// JSON:
// {
//   "type": "image",
//   "src": "imports/photo_ab12.png",   // path relative to the board folder
//   "origin": [x, y],                   // top-left in local coords
//   "size":   [w, h],                   // displayed size
//   "natural_size": [w, h],             // original pixel dimensions (optional)
//   ... base properties ...
// }
// =============================================================================

#include "WhiteboardObject.h"
#include <string>

namespace lb {

class ImageObject : public WhiteboardObject {
public:
    ImageObject();
    explicit ImageObject(UUID id);
    ImageObject(const std::string& src, Vec2f origin, Vec2f size);
    ImageObject(const std::string& src, Vec2f origin, Vec2f size, UUID id);
    ~ImageObject() override = default;

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

    Vec2f natural_size() const { return natural_size_; }
    void set_natural_size(Vec2f s) { natural_size_ = s; }

    Vec2f center() const { return origin_ + size_ * 0.5f; }

    // ---- WhiteboardObject interface ----
    AABB local_bounding_box() const override;
    bool hit_test_local(Vec2f local_point, float tolerance) const override;

    const char* type_name() const override { return "image"; }

    std::unique_ptr<WhiteboardObject> clone() const override;
    std::unique_ptr<WhiteboardObject> clone_exact() const override;

protected:
    void serialize_properties(nlohmann::json& j) const override;
    void deserialize_properties(const nlohmann::json& j) override;

private:
    std::string src_;
    Vec2f origin_;
    Vec2f size_;
    Vec2f natural_size_{0.0f, 0.0f};
};

} // namespace lb
