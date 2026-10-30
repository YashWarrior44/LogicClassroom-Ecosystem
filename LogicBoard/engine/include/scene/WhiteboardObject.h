#pragma once
// =============================================================================
// WhiteboardObject.h — Abstract base class for every drawable object on the
// LogicBoard canvas.
//
// Every concrete shape derives from this and implements:
//   - local_bounding_box()  (in untransformed/local space)
//   - hit_test_local()      (point is already in local coordinates)
//   - type_name()           (string tag used in JSON: "freehand", "line", …)
//   - serialize_properties() / deserialize_properties()
//   - clone()
//
// The base class owns the common properties: UUID, z-order, visibility,
// lock state, stroke/fill styling, opacity, and affine transform.
// It also provides the world-space bounding_box() and hit_test() wrappers
// that apply the transform before delegating to the local-space virtuals.
// =============================================================================

#include "../math/Types.h"
#include "../math/AffineTransform.h"

#include <cstdint>
#include <memory>
#include <string>

// Include nlohmann/json directly so all headers can use it.
#include <nlohmann/json.hpp>


namespace lb {

// -----------------------------------------------------------------------------
// UUID — 128-bit identifier stored as two 64-bit halves.
// Generation and string conversion are implemented in the .cpp file.
// -----------------------------------------------------------------------------
struct UUID {
    uint64_t hi = 0;
    uint64_t lo = 0;

    constexpr UUID() = default;
    constexpr UUID(uint64_t h, uint64_t l) : hi(h), lo(l) {}

    /// Generate a random v4 UUID.
    static UUID generate();

    /// Parse from "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" string.
    static UUID from_string(const std::string& str);

    /// Format as "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx".
    std::string to_string() const;

    constexpr bool is_null() const { return hi == 0 && lo == 0; }
    constexpr bool operator==(const UUID& o) const { return hi == o.hi && lo == o.lo; }
    constexpr bool operator!=(const UUID& o) const { return !(*this == o); }
    constexpr bool operator<(const UUID& o) const {
        return hi < o.hi || (hi == o.hi && lo < o.lo);
    }
};

// -----------------------------------------------------------------------------
// WhiteboardObject — Abstract base
// -----------------------------------------------------------------------------
class WhiteboardObject {
public:
    // ---- Construction / destruction ----

    WhiteboardObject();
    explicit WhiteboardObject(UUID id);
    virtual ~WhiteboardObject() = default;

    // Non-copyable (use clone()), movable.
    WhiteboardObject(const WhiteboardObject&) = delete;
    WhiteboardObject& operator=(const WhiteboardObject&) = delete;
    WhiteboardObject(WhiteboardObject&&) noexcept = default;
    WhiteboardObject& operator=(WhiteboardObject&&) noexcept = default;

    // ---- Identity ----

    const UUID& id() const { return id_; }
    void set_id(UUID new_id) { id_ = new_id; }

    // ---- Ordering ----

    int32_t z_order() const { return z_order_; }
    void set_z_order(int32_t z) { z_order_ = z; }

    // ---- Visibility / Lock ----

    bool visible() const { return visible_; }
    void set_visible(bool v) { visible_ = v; }

    bool locked() const { return locked_; }
    void set_locked(bool l) { locked_ = l; }

    // ---- Styling ----

    RGBA stroke_color() const { return stroke_color_; }
    void set_stroke_color(RGBA c) { stroke_color_ = c; }

    RGBA fill_color() const { return fill_color_; }
    void set_fill_color(RGBA c) { fill_color_ = c; }

    float stroke_width() const { return stroke_width_; }
    void set_stroke_width(float w) { stroke_width_ = w; }

    LineCap line_cap() const { return line_cap_; }
    void set_line_cap(LineCap c) { line_cap_ = c; }

    LineJoin line_join() const { return line_join_; }
    void set_line_join(LineJoin j) { line_join_ = j; }

    float opacity() const { return opacity_; }
    void set_opacity(float o) { opacity_ = (o < 0.0f) ? 0.0f : (o > 1.0f) ? 1.0f : o; }

    // ---- Transform ----

    const AffineTransform& transform() const { return transform_; }
    void set_transform(const AffineTransform& t) { transform_ = t; }

    /// Prepend a translation to the current transform.
    void translate(float dx, float dy) {
        transform_ = AffineTransform::translation(dx, dy).then(transform_);
    }

    /// Prepend a rotation (radians) around the object's local-space center.
    void rotate(float radians) {
        Vec2f c = local_bounding_box().center();
        transform_ = AffineTransform::rotation_around(radians, c).then(transform_);
    }

    /// Prepend a scale around the object's local-space center.
    void scale(float sx, float sy) {
        Vec2f c = local_bounding_box().center();
        AffineTransform s = AffineTransform::translation(c.x, c.y)
                                .then(AffineTransform::scaling(sx, sy))
                                .then(AffineTransform::translation(-c.x, -c.y));
        transform_ = s.then(transform_);
    }

    // ---- Geometry queries ----

    /// Bounding box in local (untransformed) coordinates.
    virtual AABB local_bounding_box() const = 0;

    /// Bounding box in world coordinates (applies the transform).
    AABB bounding_box() const {
        return transform_.apply(local_bounding_box());
    }

    /// Hit-test in local coordinates. `tolerance` is in local-space units
    /// (the caller scales it by the inverse transform if needed).
    virtual bool hit_test_local(Vec2f local_point, float tolerance) const = 0;

    /// Hit-test in world coordinates.
    bool hit_test(Vec2f world_point, float tolerance) const {
        Vec2f local = transform_.inverse().apply(world_point);
        return hit_test_local(local, tolerance);
    }

    // ---- Type tag (for JSON serialization) ----

    /// Returns the string tag written to JSON: "freehand", "highlighter",
    /// "line", "rect", "ellipse", "arrow", "polygon", "image", "ppt".
    virtual const char* type_name() const = 0;

    // ---- Serialization (JSON) ----

    /// Serialize the complete object (base + derived properties) to JSON.
    nlohmann::json serialize() const;

    /// Deserialize base properties from JSON. Called by the factory before
    /// the derived class reads its own properties.
    void deserialize_base(const nlohmann::json& j);

    /// Factory: create the right derived type from a JSON blob.
    static std::unique_ptr<WhiteboardObject> from_json(const nlohmann::json& j);

    // ---- Clone ----

    /// Deep-copy this object (new UUID is generated).
    virtual std::unique_ptr<WhiteboardObject> clone() const = 0;

    /// Deep-copy preserving the same UUID (used for undo snapshots).
    virtual std::unique_ptr<WhiteboardObject> clone_exact() const = 0;

    /// Approximate heap memory used by this object (for command stack budgeting).
    /// Default returns a small fixed amount; override for objects with large
    /// dynamic data (point arrays, text strings).
    virtual size_t memory_estimate() const { return 128; }

protected:
    /// Write shape-specific properties into the JSON object `j`.
    /// Called by serialize() after base properties are written.
    virtual void serialize_properties(nlohmann::json& j) const = 0;

    /// Read shape-specific properties from the JSON object `j`.
    /// Called by from_json() after the base is deserialized.
    virtual void deserialize_properties(const nlohmann::json& j) = 0;

    /// Helper: copy all base-class fields from `src` into `this`.
    void copy_base_from(const WhiteboardObject& src);

private:
    UUID            id_;
    int32_t         z_order_      = 0;
    bool            visible_      = true;
    bool            locked_       = false;

    RGBA            stroke_color_ = RGBA::blue();
    RGBA            fill_color_   = RGBA::transparent();
    float           stroke_width_ = 3.0f;
    LineCap         line_cap_     = LineCap::Round;
    LineJoin        line_join_    = LineJoin::Round;
    float           opacity_      = 1.0f;

    AffineTransform transform_;
};

} // namespace lb