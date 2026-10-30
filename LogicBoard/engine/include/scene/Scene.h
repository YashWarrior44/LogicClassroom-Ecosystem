#pragma once
// =============================================================================
// Scene.h — The object container for a single whiteboard page.
//
// A Scene owns a z-ordered collection of WhiteboardObject instances.  It is
// the single source of truth for what exists on a page and provides:
//   - Add / remove / reorder objects
//   - Spatial queries (point hit-test, rectangle selection)
//   - Full-scene JSON serialization and deserialization
//   - Z-order management (bring-to-front, send-to-back, etc.)
//
// Thread safety: Scene is NOT thread-safe.  All mutation must happen on
// the same thread (the UI thread in the final app).
//
// JSON document format (human-readable, version-tagged):
// {
//   "logicboard_version": "3.0",
//   "scene_version": 1,
//   "canvas_width": 1920,
//   "canvas_height": 1080,
//   "background": "#000000",
//   "objects": [
//     { "type": "freehand", "id": "...", ... },
//     { "type": "rect",     "id": "...", ... },
//     ...
//   ]
// }
// =============================================================================

#include "WhiteboardObject.h"
#include "../math/Types.h"

#include <cstdint>
#include <memory>
#include <string>
#include <vector>
#include <deque>
#include <functional>
#include <optional>

namespace lb {

class Scene {
public:
    // ---- Construction ----

    Scene();
    Scene(int32_t canvas_width, int32_t canvas_height);
    ~Scene() = default;

    // Movable, not copyable (use deep_copy() for explicit cloning).
    Scene(const Scene&) = delete;
    Scene& operator=(const Scene&) = delete;
    Scene(Scene&&) noexcept = default;
    Scene& operator=(Scene&&) noexcept = default;

    // ---- Canvas metadata ----

    int32_t canvas_width() const { return canvas_width_; }
    int32_t canvas_height() const { return canvas_height_; }
    void set_canvas_size(int32_t w, int32_t h) { canvas_width_ = w; canvas_height_ = h; }

    RGBA background_color() const { return background_; }
    void set_background_color(RGBA c) { background_ = c; }

    // ---- Background image (optional) ----
    // A board-relative src path (e.g. "imports/bg_a1b2.png"). Empty = none.
    // Drawn behind all objects, clipped to the canvas. Per-page.
    const std::string& bg_image() const { return bg_image_; }
    void set_bg_image(const std::string& src) { bg_image_ = src; }
    bool has_bg_image() const { return !bg_image_.empty(); }
    void clear_bg_image() { bg_image_.clear(); }

    // ---- Object count ----

    size_t object_count() const { return objects_.size(); }
    bool empty() const { return objects_.empty(); }

    // ---- Access by index (z-order sorted, 0 = backmost) ----

    /// Read-only access to the object at z-index position `i`.
    const WhiteboardObject* object_at(size_t i) const;

    /// Mutable access (use with care — caller must not delete the pointer).
    WhiteboardObject* object_at_mut(size_t i);

    // ---- Access by UUID ----

    /// Find an object by its UUID.  Returns nullptr if not found.
    const WhiteboardObject* find(const UUID& id) const;
    WhiteboardObject* find_mut(const UUID& id);

    /// Returns the z-index of the object with the given UUID, or -1.
    int find_index(const UUID& id) const;

    // ---- Iteration ----

    /// Iterate all objects in z-order (back to front).
    using ObjectVisitor = std::function<void(const WhiteboardObject&)>;
    void for_each(ObjectVisitor visitor) const;

    /// Iterate all objects in z-order (mutable).
    using MutableObjectVisitor = std::function<void(WhiteboardObject&)>;
    void for_each_mut(MutableObjectVisitor visitor);

    /// Collect raw pointers to all objects (z-order, back to front).
    std::vector<const WhiteboardObject*> all_objects() const;

    // ---- Add / Remove ----

    /// Add an object to the scene.  The scene takes ownership.
    /// The object is placed at the top of the z-order.
    /// Returns a non-owning pointer to the added object.
    WhiteboardObject* add(std::unique_ptr<WhiteboardObject> obj);

    /// Add an object at a specific z-index position.
    WhiteboardObject* add_at(std::unique_ptr<WhiteboardObject> obj, size_t z_index);

    /// Remove and return the object with the given UUID.
    /// Returns nullptr if not found.
    std::unique_ptr<WhiteboardObject> remove(const UUID& id);

    /// Remove the object at z-index `i` and return it.
    std::unique_ptr<WhiteboardObject> remove_at(size_t i);

    /// Remove all objects.
    void clear();

    // ---- Z-order manipulation ----

    /// Move an object to the top of the z-order (renders last = visually on top).
    void bring_to_front(const UUID& id);

    /// Move an object to the bottom of the z-order.
    void send_to_back(const UUID& id);

    /// Move an object one step up in z-order.
    void bring_forward(const UUID& id);

    /// Move an object one step down in z-order.
    void send_backward(const UUID& id);

    /// Reassign z_order integer values to match the current vector positions.
    /// Called automatically after reorder operations.
    void normalize_z_order();

    // ---- Spatial queries ----

    /// Find the topmost object (highest z-order) hit by a point.
    /// `tolerance` is the maximum distance in canvas units for near-miss hits
    /// (useful for thin lines).  Returns nullptr if nothing is hit.
    const WhiteboardObject* hit_test(Vec2f world_point, float tolerance = 4.0f) const;

    /// Find ALL objects hit by a point, returned in z-order (back to front).
    std::vector<const WhiteboardObject*> hit_test_all(Vec2f world_point, float tolerance = 4.0f) const;

    /// Find all objects whose bounding box intersects the given rectangle.
    /// Useful for lasso / rectangle selection.
    std::vector<const WhiteboardObject*> query_rect(const AABB& rect) const;

    /// Find all objects whose bounding box is fully contained within the rect.
    std::vector<const WhiteboardObject*> query_rect_contained(const AABB& rect) const;

    // ---- Serialization (JSON) ----

    /// Serialize the entire scene to a JSON object.
    nlohmann::json serialize() const;

    /// Serialize the entire scene to a formatted JSON string.
    std::string serialize_to_string(int indent = 2) const;

    /// Deserialize a scene from a JSON object.  Replaces all current content.
    /// Returns true on success, false if the JSON is malformed.
    bool deserialize(const nlohmann::json& j);

    /// Deserialize from a JSON string.
    bool deserialize_from_string(const std::string& json_str);

    /// Save the scene to a file on disk (UTF-8, pretty-printed).
    bool save_to_file(const std::string& path, int indent = 2) const;

    /// Load a scene from a JSON file on disk.
    bool load_from_file(const std::string& path);

    // ---- Deep copy ----

    /// Create an independent deep copy of this scene (all objects cloned
    /// with new UUIDs).
    Scene deep_copy() const;

    /// Create a deep copy preserving all original UUIDs (for undo snapshots).
    Scene deep_copy_exact() const;

    // ---- Dirty tracking ----

    /// True if the scene has been modified since the last clear_dirty() call.
    bool is_dirty() const { return dirty_; }
    void mark_dirty() { dirty_ = true; }
    void clear_dirty() { dirty_ = false; }

    // ---- Version info baked into JSON ----

    static constexpr const char* LOGICBOARD_VERSION = "3.0";
    static constexpr int SCENE_FORMAT_VERSION = 1;

private:
    int32_t canvas_width_  = 1920;
    int32_t canvas_height_ = 1080;
    RGBA    background_    = RGBA::black();
    std::string bg_image_;  // board-relative path; empty = none
    bool    dirty_         = false;

    /// Objects stored in z-order: index 0 = backmost, last = frontmost.
    std::vector<std::unique_ptr<WhiteboardObject>> objects_;
};

// =============================================================================
// Multi-page board document (a board = ordered list of scenes/pages).
//
// JSON:
// {
//   "logicboard_version": "3.0",
//   "board_name": "My Board",
//   "theme": "dark",
//   "last_page": 0,
//   "pages": [
//     { <Scene JSON> },
//     { <Scene JSON> },
//     ...
//   ]
// }
// =============================================================================

class Board {
public:
    Board();
    explicit Board(const std::string& name);
    ~Board() = default;

    Board(Board&&) noexcept = default;
    Board& operator=(Board&&) noexcept = default;
    Board(const Board&) = delete;
    Board& operator=(const Board&) = delete;

    // ---- Metadata ----

    const std::string& name() const { return name_; }
    void set_name(const std::string& n) { name_ = n; }

    const std::string& theme() const { return theme_; }
    void set_theme(const std::string& t) { theme_ = t; }

    int32_t last_page() const { return last_page_; }
    void set_last_page(int32_t p) { last_page_ = p; }

    // ---- Pages ----

    size_t page_count() const { return pages_.size(); }
    bool empty() const { return pages_.empty(); }

    /// Access a page by index.
    const Scene& page(size_t i) const { return pages_.at(i); }
    Scene& page_mut(size_t i) { return pages_.at(i); }

    /// Add a blank page at the end.
    Scene& add_page();

    /// Add a blank page at a specific index.
    Scene& add_page_at(size_t index);

    /// Insert an existing scene as a page.
    void insert_page(size_t index, Scene scene);

    /// Remove and return the page at index `i`.
    Scene remove_page(size_t i);

    /// Swap two pages by index.
    void swap_pages(size_t a, size_t b);

    // ---- Serialization ----

    /// Serialize the entire board (all pages) to JSON.
    nlohmann::json serialize() const;

    /// Serialize to formatted JSON string.
    std::string serialize_to_string(int indent = 2) const;

    /// Deserialize from JSON.  Replaces all current pages.
    bool deserialize(const nlohmann::json& j);

    /// Deserialize from a JSON string.
    bool deserialize_from_string(const std::string& json_str);

    /// Save to a single JSON file.
    bool save_to_file(const std::string& path, int indent = 2) const;

    /// Load from a JSON file.
    bool load_from_file(const std::string& path);

private:
    std::string          name_  = "Untitled";
    std::string          theme_ = "dark";
    int32_t              last_page_ = 0;
    std::deque<Scene>    pages_;  // deque: stable element addresses across add_page()
};

} // namespace lb