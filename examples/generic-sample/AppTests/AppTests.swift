import XCTest
@testable import GenApp
final class AppTests: XCTestCase { func testAnswer() { XCTAssertEqual(Other.answer() > 0, true) } }
