// =============================================================================
// Scene.cpp — Implementation of Scene (single-page object container) and
// Board (multi-page document wrapper).
//
// JSON I/O uses nlohmann/json with pretty-printing for human readability.
// File operations use standard C++ fstream — no platform dependencies.
// =============================================================================

#include "../../include/scene/Scene.h"
#include "../../include/scene/WhiteboardObject.h"

#include <nlohmann/json.hpp>

#include <fstream>
#include <algorithm>
#include <stdexcept>
#include <sstream>

using json = nlohmann::json;

namespace lb {

// =============================================================================
// Scene — Construction
// =============================================================================

Scene::Scene()
    : canvas_width_(1920), canvas_height_(1080),
      background_(RGBA::black()), dirty_(false) {}

Scene::Scene(int32_t canvas_width, int32_t canvas_height)
    : canvas_width_(canvas_width), canvas_height_(canvas_height),
      background_(RGBA::black()), dirty_(false) {}

// =============================================================================
// Scene — Access by index
// =============================================================================

const WhiteboardObject* Scene::object_at(size_t i) const {
    if (i >= objects_.size()) return nullptr;
    return objects_[i].get();
}

WhiteboardObject* Scene::object_at_mut(size_t i) {
    if (i >= objects_.size()) return nullptr;
    return objects_[i].get();
}

// =============================================================================
// Scene — Access by UUID
// =============================================================================

const WhiteboardObject* Scene::find(const UUID& id) const {
    for (const auto& obj : objects_) {
        if (obj->id() == id) return obj.get();
    }
    return nullptr;
}

WhiteboardObject* Scene::find_mut(const UUID& id) {
    for (auto& obj : objects_) {
        if (obj->id() == id) return obj.get();
    }
    return nullptr;
}

int Scene::find_index(const UUID& id) const {
    for (size_t i = 0; i < objects_.size(); ++i) {
        if (objects_[i]->id() == id) return static_cast<int>(i);
    }
    return -1;
}

// =============================================================================
// Scene — Iteration
// =============================================================================

void Scene::for_each(ObjectVisitor visitor) const {
    for (const auto& obj : objects_) {
        visitor(*obj);
    }
}

void Scene::for_each_mut(MutableObjectVisitor visitor) {
    for (auto& obj : objects_) {
        visitor(*obj);
    }
}

std::vector<const WhiteboardObject*> Scene::all_objects() const {
    std::vector<const WhiteboardObject*> result;
    result.reserve(objects_.size());
    for (const auto& obj : objects_) {
        result.push_back(obj.get());
    }
    return result;
}

// =============================================================================
// Scene — Add / Remove
// =============================================================================

WhiteboardObject* Scene::add(std::unique_ptr<WhiteboardObject> obj) {
    if (!obj) return nullptr;

    // Assign z_order to be on top of everything.
    int32_t max_z = 0;
    for (const auto& existing : objects_) {
        if (existing->z_order() >= max_z) {
            max_z = existing->z_order() + 1;
        }
    }
    obj->set_z_order(max_z);

    WhiteboardObject* ptr = obj.get();
    objects_.push_back(std::move(obj));
    dirty_ = true;
    return ptr;
}

WhiteboardObject* Scene::add_at(std::unique_ptr<WhiteboardObject> obj, size_t z_index) {
    if (!obj) return nullptr;

    z_index = std::min(z_index, objects_.size());

    WhiteboardObject* ptr = obj.get();
    objects_.insert(objects_.begin() + static_cast<ptrdiff_t>(z_index), std::move(obj));

    normalize_z_order();
    dirty_ = true;
    return ptr;
}

std::unique_ptr<WhiteboardObject> Scene::remove(const UUID& id) {
    for (auto it = objects_.begin(); it != objects_.end(); ++it) {
        if ((*it)->id() == id) {
            auto removed = std::move(*it);
            objects_.erase(it);
            normalize_z_order();
            dirty_ = true;
            return removed;
        }
    }
    return nullptr;
}

std::unique_ptr<WhiteboardObject> Scene::remove_at(size_t i) {
    if (i >= objects_.size()) return nullptr;

    auto removed = std::move(objects_[i]);
    objects_.erase(objects_.begin() + static_cast<ptrdiff_t>(i));
    normalize_z_order();
    dirty_ = true;
    return removed;
}

void Scene::clear() {
    objects_.clear();
    dirty_ = true;
}

// =============================================================================
// Scene — Z-order manipulation
// =============================================================================

void Scene::bring_to_front(const UUID& id) {
    int idx = find_index(id);
    if (idx < 0 || idx == static_cast<int>(objects_.size()) - 1) return;

    auto obj = std::move(objects_[static_cast<size_t>(idx)]);
    objects_.erase(objects_.begin() + idx);
    objects_.push_back(std::move(obj));
    normalize_z_order();
    dirty_ = true;
}

void Scene::send_to_back(const UUID& id) {
    int idx = find_index(id);
    if (idx <= 0) return;

    auto obj = std::move(objects_[static_cast<size_t>(idx)]);
    objects_.erase(objects_.begin() + idx);
    objects_.insert(objects_.begin(), std::move(obj));
    normalize_z_order();
    dirty_ = true;
}

void Scene::bring_forward(const UUID& id) {
    int idx = find_index(id);
    if (idx < 0 || idx == static_cast<int>(objects_.size()) - 1) return;

    std::swap(objects_[static_cast<size_t>(idx)],
              objects_[static_cast<size_t>(idx) + 1]);
    normalize_z_order();
    dirty_ = true;
}

void Scene::send_backward(const UUID& id) {
    int idx = find_index(id);
    if (idx <= 0) return;

    std::swap(objects_[static_cast<size_t>(idx)],
              objects_[static_cast<size_t>(idx) - 1]);
    normalize_z_order();
    dirty_ = true;
}

void Scene::normalize_z_order() {
    for (size_t i = 0; i < objects_.size(); ++i) {
        objects_[i]->set_z_order(static_cast<int32_t>(i));
    }
}

// =============================================================================
// Scene — Spatial queries
// =============================================================================

const WhiteboardObject* Scene::hit_test(Vec2f world_point, float tolerance) const {
    // Iterate back-to-front; first hit from top is the topmost visible object.
    for (auto it = objects_.rbegin(); it != objects_.rend(); ++it) {
        const auto& obj = *it;
        if (!obj->visible()) continue;
        if (obj->hit_test(world_point, tolerance)) {
            return obj.get();
        }
    }
    return nullptr;
}

std::vector<const WhiteboardObject*> Scene::hit_test_all(Vec2f world_point, float tolerance) const {
    std::vector<const WhiteboardObject*> hits;
    for (const auto& obj : objects_) {
        if (!obj->visible()) continue;
        if (obj->hit_test(world_point, tolerance)) {
            hits.push_back(obj.get());
        }
    }
    return hits;
}

std::vector<const WhiteboardObject*> Scene::query_rect(const AABB& rect) const {
    std::vector<const WhiteboardObject*> results;
    for (const auto& obj : objects_) {
        if (!obj->visible()) continue;
        if (obj->bounding_box().intersects(rect)) {
            results.push_back(obj.get());
        }
    }
    return results;
}

std::vector<const WhiteboardObject*> Scene::query_rect_contained(const AABB& rect) const {
    std::vector<const WhiteboardObject*> results;
    for (const auto& obj : objects_) {
        if (!obj->visible()) continue;
        AABB bb = obj->bounding_box();
        if (rect.contains(bb.min_pt) && rect.contains(bb.max_pt)) {
            results.push_back(obj.get());
        }
    }
    return results;
}

// =============================================================================
// Scene — Serialization
// =============================================================================

json Scene::serialize() const {
    json j;

    j["logicboard_version"] = LOGICBOARD_VERSION;
    j["scene_version"]      = SCENE_FORMAT_VERSION;
    j["canvas_width"]       = canvas_width_;
    j["canvas_height"]      = canvas_height_;
    j["background"]         = background_.to_hex_string();
    if (!bg_image_.empty()) j["bg_image"] = bg_image_;

    json arr = json::array();
    for (const auto& obj : objects_) {
        arr.push_back(obj->serialize());
    }
    j["objects"] = std::move(arr);

    return j;
}

std::string Scene::serialize_to_string(int indent) const {
    return serialize().dump(indent);
}

bool Scene::deserialize(const json& j) {
    // Clear existing content before loading.
    objects_.clear();
    dirty_ = false;

    // Canvas metadata (with safe defaults).
    if (j.contains("canvas_width") && j["canvas_width"].is_number()) {
        canvas_width_ = j["canvas_width"].get<int32_t>();
    }
    if (j.contains("canvas_height") && j["canvas_height"].is_number()) {
        canvas_height_ = j["canvas_height"].get<int32_t>();
    }
    bg_image_.clear();
    if (j.contains("bg_image") && j["bg_image"].is_string()) {
        bg_image_ = j["bg_image"].get<std::string>();
    }
    if (j.contains("background") && j["background"].is_string()) {
        std::string bg_str = j["background"].get<std::string>();
        // Reuse the color parser from WhiteboardObject.cpp — inline here for
        // self-contained compilation.
        if (bg_str.size() >= 7 && bg_str[0] == '#') {
            uint32_t val = 0;
            bool ok = true;
            for (size_t i = 1; i < bg_str.size() && i <= 8; ++i) {
                char c = bg_str[i];
                uint8_t nibble;
                if (c >= '0' && c <= '9')      nibble = static_cast<uint8_t>(c - '0');
                else if (c >= 'a' && c <= 'f') nibble = static_cast<uint8_t>(c - 'a' + 10);
                else if (c >= 'A' && c <= 'F') nibble = static_cast<uint8_t>(c - 'A' + 10);
                else { ok = false; break; }
                val = (val << 4) | nibble;
            }
            if (ok) {
                if (bg_str.size() == 7) {
                    background_ = RGBA{
                        static_cast<uint8_t>((val >> 16) & 0xFF),
                        static_cast<uint8_t>((val >> 8)  & 0xFF),
                        static_cast<uint8_t>( val        & 0xFF),
                        255
                    };
                } else if (bg_str.size() == 9) {
                    background_ = RGBA{
                        static_cast<uint8_t>((val >> 24) & 0xFF),
                        static_cast<uint8_t>((val >> 16) & 0xFF),
                        static_cast<uint8_t>((val >> 8)  & 0xFF),
                        static_cast<uint8_t>( val        & 0xFF)
                    };
                }
            }
        }
    }

    // Objects array.
    if (j.contains("objects") && j["objects"].is_array()) {
        const auto& arr = j["objects"];
        objects_.reserve(arr.size());

        for (const auto& obj_json : arr) {
            // (bg_image handled above)
            auto obj = WhiteboardObject::from_json(obj_json);
            if (obj) {
                objects_.push_back(std::move(obj));
            }
            // Silently skip objects with unknown type — forward compatibility.
        }
    }

    // Ensure z_order values are consistent with vector positions.
    normalize_z_order();

    return true;
}

bool Scene::deserialize_from_string(const std::string& json_str) {
    try {
        json j = json::parse(json_str);
        return deserialize(j);
    } catch (const json::parse_error&) {
        return false;
    }
}

// =============================================================================
// Scene — File I/O
// =============================================================================

bool Scene::save_to_file(const std::string& path, int indent) const {
    try {
        std::ofstream ofs(path, std::ios::out | std::ios::trunc);
        if (!ofs.is_open()) return false;

        ofs << serialize().dump(indent);
        ofs.flush();

        return ofs.good();
    } catch (...) {
        return false;
    }
}

bool Scene::load_from_file(const std::string& path) {
    try {
        std::ifstream ifs(path, std::ios::in);
        if (!ifs.is_open()) return false;

        // Read entire file into string.
        std::ostringstream ss;
        ss << ifs.rdbuf();
        std::string content = ss.str();

        if (content.empty()) return false;

        json j = json::parse(content);
        return deserialize(j);
    } catch (const json::parse_error&) {
        return false;
    } catch (...) {
        return false;
    }
}

// =============================================================================
// Scene — Deep copy
// =============================================================================

Scene Scene::deep_copy() const {
    Scene copy(canvas_width_, canvas_height_);
    copy.background_ = background_;
    copy.bg_image_ = bg_image_;

    copy.objects_.reserve(objects_.size());
    for (const auto& obj : objects_) {
        copy.objects_.push_back(obj->clone());
    }
    copy.normalize_z_order();
    copy.dirty_ = false;

    return copy;
}

Scene Scene::deep_copy_exact() const {
    Scene copy(canvas_width_, canvas_height_);
    copy.background_ = background_;
    copy.bg_image_ = bg_image_;

    copy.objects_.reserve(objects_.size());
    for (const auto& obj : objects_) {
        copy.objects_.push_back(obj->clone_exact());
    }
    copy.normalize_z_order();
    copy.dirty_ = false;

    return copy;
}

// =============================================================================
// Board — Construction
// =============================================================================

Board::Board() {
    // Start with one blank page.
    pages_.emplace_back();
}

Board::Board(const std::string& name) : name_(name) {
    pages_.emplace_back();
}

// =============================================================================
// Board — Page management
// =============================================================================

Scene& Board::add_page() {
    pages_.emplace_back();
    return pages_.back();
}

Scene& Board::add_page_at(size_t index) {
    index = std::min(index, pages_.size());
    auto it = pages_.emplace(pages_.begin() + static_cast<ptrdiff_t>(index));
    return *it;
}

void Board::insert_page(size_t index, Scene scene) {
    index = std::min(index, pages_.size());
    pages_.insert(pages_.begin() + static_cast<ptrdiff_t>(index), std::move(scene));
}

Scene Board::remove_page(size_t i) {
    if (i >= pages_.size()) {
        throw std::out_of_range("Board::remove_page: index out of range");
    }
    Scene removed = std::move(pages_[i]);
    pages_.erase(pages_.begin() + static_cast<ptrdiff_t>(i));

    // Always keep at least one page.
    if (pages_.empty()) {
        pages_.emplace_back();
    }

    // Adjust last_page_ if it now points past the end.
    if (last_page_ >= static_cast<int32_t>(pages_.size())) {
        last_page_ = static_cast<int32_t>(pages_.size()) - 1;
    }

    return removed;
}

void Board::swap_pages(size_t a, size_t b) {
    if (a < pages_.size() && b < pages_.size() && a != b) {
        std::swap(pages_[a], pages_[b]);
    }
}

// =============================================================================
// Board — Serialization
// =============================================================================

json Board::serialize() const {
    json j;

    j["logicboard_version"] = Scene::LOGICBOARD_VERSION;
    j["board_name"]         = name_;
    j["theme"]              = theme_;
    j["last_page"]          = last_page_;

    json pages_arr = json::array();
    for (const auto& page : pages_) {
        pages_arr.push_back(page.serialize());
    }
    j["pages"] = std::move(pages_arr);

    return j;
}

std::string Board::serialize_to_string(int indent) const {
    return serialize().dump(indent);
}

bool Board::deserialize(const json& j) {
    pages_.clear();
    last_page_ = 0;

    // Metadata.
    if (j.contains("board_name") && j["board_name"].is_string()) {
        name_ = j["board_name"].get<std::string>();
    }
    if (j.contains("theme") && j["theme"].is_string()) {
        theme_ = j["theme"].get<std::string>();
    }
    if (j.contains("last_page") && j["last_page"].is_number_integer()) {
        last_page_ = j["last_page"].get<int32_t>();
    }

    // Pages.
    if (j.contains("pages") && j["pages"].is_array()) {
        const auto& pages_arr = j["pages"];

        for (const auto& page_json : pages_arr) {
            Scene scene;
            if (scene.deserialize(page_json)) {
                pages_.push_back(std::move(scene));
            }
        }
    }

    // Ensure at least one page exists.
    if (pages_.empty()) {
        pages_.emplace_back();
    }

    // Clamp last_page to valid range.
    if (last_page_ < 0) last_page_ = 0;
    if (last_page_ >= static_cast<int32_t>(pages_.size())) {
        last_page_ = static_cast<int32_t>(pages_.size()) - 1;
    }

    return true;
}

bool Board::deserialize_from_string(const std::string& json_str) {
    try {
        json j = json::parse(json_str);
        return deserialize(j);
    } catch (const json::parse_error&) {
        return false;
    }
}

// =============================================================================
// Board — File I/O
// =============================================================================

bool Board::save_to_file(const std::string& path, int indent) const {
    try {
        std::ofstream ofs(path, std::ios::out | std::ios::trunc);
        if (!ofs.is_open()) return false;

        ofs << serialize().dump(indent);
        ofs.flush();

        return ofs.good();
    } catch (...) {
        return false;
    }
}

bool Board::load_from_file(const std::string& path) {
    try {
        std::ifstream ifs(path, std::ios::in);
        if (!ifs.is_open()) return false;

        std::ostringstream ss;
        ss << ifs.rdbuf();
        std::string content = ss.str();

        if (content.empty()) return false;

        json j = json::parse(content);
        return deserialize(j);
    } catch (const json::parse_error&) {
        return false;
    } catch (...) {
        return false;
    }
}

} // namespace lb