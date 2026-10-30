// test_scene.cpp — Google Test suite for Scene
// Build with: cmake --build build --target tests

#include <gtest/gtest.h>
#include <scene/Scene.h>
#include <scene/FreehandStroke.h>
#include <scene/RectObject.h>

using namespace lb;

TEST(SceneTest, StartsEmpty) {
    Scene scene(800, 600);
    EXPECT_EQ(scene.object_count(), 0);
    EXPECT_TRUE(scene.empty());
}

TEST(SceneTest, AddRemove) {
    Scene scene(800, 600);
    auto stroke = std::make_unique<FreehandStroke>();
    stroke->add_point({10, 20});
    UUID id = stroke->id();

    scene.add(std::move(stroke));
    EXPECT_EQ(scene.object_count(), 1);

    auto removed = scene.remove(id);
    EXPECT_NE(removed, nullptr);
    EXPECT_EQ(scene.object_count(), 0);
}

TEST(SceneTest, HitTest) {
    Scene scene(800, 600);
    auto stroke = std::make_unique<FreehandStroke>();
    stroke->set_stroke_width(5.0f);
    stroke->add_point({100, 100});
    stroke->add_point({200, 100});
    scene.add(std::move(stroke));

    auto* hit = scene.hit_test({150, 100}, 4.0f);
    EXPECT_NE(hit, nullptr);

    auto* miss = scene.hit_test({500, 500}, 4.0f);
    EXPECT_EQ(miss, nullptr);
}

TEST(SceneTest, JsonRoundTrip) {
    Scene scene(800, 600);
    auto rect = std::make_unique<RectObject>(Vec2f{10, 20}, Vec2f{100, 50});
    scene.add(std::move(rect));

    std::string json = scene.serialize_to_string();
    EXPECT_FALSE(json.empty());

    Scene scene2;
    EXPECT_TRUE(scene2.deserialize_from_string(json));
    EXPECT_EQ(scene2.object_count(), 1);
}
