#include <gtest/gtest.h>
#include <scene/Scene.h>
#include <scene/FreehandStroke.h>
#include <scene/HighlighterStroke.h>
#include <scene/LineObject.h>
#include <scene/RectObject.h>
#include <scene/EllipseObject.h>
#include <scene/ArrowObject.h>
#include <scene/PolygonObject.h>
#include <scene/TextObject.h>

using namespace lb;

TEST(SerializerTest, AllShapeTypes) {
    Scene scene(1920, 1080);

    auto fs = std::make_unique<FreehandStroke>();
    fs->add_point({1, 2}); fs->add_point({3, 4});
    scene.add(std::move(fs));

    auto hs = std::make_unique<HighlighterStroke>();
    hs->add_point({5, 6}); hs->add_point({7, 8});
    scene.add(std::move(hs));

    scene.add(std::make_unique<LineObject>(Vec2f{0,0}, Vec2f{100,100}));
    scene.add(std::make_unique<RectObject>(Vec2f{10,10}, Vec2f{50,30}));
    scene.add(std::make_unique<EllipseObject>(Vec2f{200,200}, Vec2f{40,20}));
    scene.add(std::make_unique<ArrowObject>(Vec2f{0,0}, Vec2f{80,0}));

    auto pg = std::make_unique<PolygonObject>();
    pg->add_vertex({0,0}); pg->add_vertex({50,0}); pg->add_vertex({25,40});
    scene.add(std::move(pg));

    scene.add(std::make_unique<TextObject>("Hello", Vec2f{300,300}, 16.0f));

    EXPECT_EQ(scene.object_count(), 8);

    std::string json = scene.serialize_to_string();

    Scene scene2;
    EXPECT_TRUE(scene2.deserialize_from_string(json));
    EXPECT_EQ(scene2.object_count(), 8);

    // Verify types survived round-trip
    EXPECT_STREQ(scene2.object_at(0)->type_name(), "freehand");
    EXPECT_STREQ(scene2.object_at(1)->type_name(), "highlighter");
    EXPECT_STREQ(scene2.object_at(2)->type_name(), "line");
    EXPECT_STREQ(scene2.object_at(7)->type_name(), "text");
}

TEST(SerializerTest, BoardRoundTrip) {
    Board board("TestBoard");
    board.set_theme("dark");
    board.add_page();

    auto& p0 = board.page_mut(0);
    p0.add(std::make_unique<RectObject>(Vec2f{0,0}, Vec2f{100,100}));

    auto& p1 = board.page_mut(1);
    p1.add(std::make_unique<LineObject>(Vec2f{0,0}, Vec2f{50,50}));

    std::string json = board.serialize_to_string();

    Board board2;
    EXPECT_TRUE(board2.deserialize_from_string(json));
    EXPECT_EQ(board2.name(), "TestBoard");
    EXPECT_EQ(board2.page_count(), 2);
    EXPECT_EQ(board2.page(0).object_count(), 1);
    EXPECT_EQ(board2.page(1).object_count(), 1);
}
