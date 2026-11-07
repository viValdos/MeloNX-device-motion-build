#!/usr/bin/env python3
"""Validate the patch and, on a Mac, exercise the real Swift motion/model code.

Usage: python3 verify-motion.py /absolute/path/to/MeloNX [--static-only]
The Swift tests use sensor/bridge test doubles, not real Apple hardware.
No files in the repository are modified. Python standard library only.
"""
import argparse
import json
import math
import pathlib
import re
import subprocess
import tempfile

BASE = "13700519f3abc4e6d7ed8a065b118b2cc59bcd67"
APP = "src/MeloNX/MeloNX/"
MOTION = APP + "Core/Controllers/Motion/BaseController+Motion.swift"
MODEL = APP + "Core/Models/ControllerMapping.swift"


def static_checks(repo):
    def original(path):
        return subprocess.check_output(["git", "show", BASE + ":" + path], cwd=repo).decode()

    current = (repo / MOTION).read_text(encoding="utf-8")
    baseline = original(MOTION)
    # Check the actual remap and orientation implementation, not a copied formula.
    marker = "    private func remapToSwitchCoords"
    assert current[current.index(marker):] == baseline[baseline.index(marker):]
    for name in ("rawAccel", "rawGyro", "rawGyroDeg"):
        pattern = rf"let {name} = SIMD3<Float>\((.*?)\n\s*\)([^\n]*)"
        old = re.search(pattern, baseline, re.S).group(0)
        new = re.search(pattern, current, re.S).group(0)
        assert re.sub(r"\s+", "", old) == re.sub(r"\s+", "", new), name
    for line in ("let accel = SIMD3(-rawAccelX, -rawAccelZ, rawAccelY)",
                 "let gyro = SIMD3(rawGyroDeg.x, rawGyroDeg.z, -rawGyroDeg.y)"):
        assert line in current and line in baseline
    assert "deviceMotionUpdateInterval = 1.0 / 60.0" in current
    base_controller = (repo / (APP + "Core/Controllers/BaseController.swift")).read_text()
    assert "motionSubmitInterval: UInt64 = 10_000_000" in base_controller

    # Evaluate the actual landscape coordinate expressions on basis vectors.
    for orientation, expected in {
        "landscapeLeft": [(0, -1, 0), (1, 0, 0), (0, 0, 1)],
        "landscapeRight": [(0, 1, 0), (-1, 0, 0), (0, 0, 1)],
    }.items():
        block = current.split("case ." + orientation + ":", 1)[1].split("case ", 1)[0]
        for var, target in (("accel", "finalAccel"), ("gyro", "finalGyro")):
            expression = re.search(rf"{target} = SIMD3<Float>\(([^)]+)\)", block).group(1)
            terms = [term.strip() for term in expression.split(",")]
            for index in range(3):
                basis = [int(i == index) for i in range(3)]
                actual = tuple((-1 if term.startswith("-") else 1) *
                               basis["xyz".index(term[-1])] for term in terms)
                assert actual == expected[index], (orientation, var, basis, actual)
                assert math.isclose(sum(v*v for v in actual), 1)
    print("PASS: unchanged units/signs/remap/orientation code, landscape basis vectors, sample rates")

    catalog = json.loads((repo / (APP + "Assets/Localizable.xcstrings")).read_text(encoding="utf-8"))
    assert "Use device gyro with controller" in catalog["strings"]
    assert all(s in catalog["strings"] for s in ["Auto", "Always", "Off"])
    assert any(s.startswith("Auto uses controller motion when supported") for s in catalog["strings"])
    subprocess.run(["git", "diff", "--check"], cwd=repo, check=True)
    changed = subprocess.check_output(["git", "diff", BASE, "--name-only"], cwd=repo, text=True).splitlines()
    assert changed and all(p.startswith(APP) and (p.endswith(".swift") or p.endswith(".xcstrings")) for p in changed)
    print("PASS: string catalog, patch whitespace, Swift/UI-only change boundary")


STUBS = r'''
import Foundation

// Test doubles only. Production framework imports are removed from the copied
// source below so its routing, conversion, Codable, and callback logic can run.
enum UIDeviceOrientation {
    case unknown, portrait, portraitUpsideDown, landscapeLeft, landscapeRight, faceUp, faceDown
    var isValidInterfaceOrientation: Bool {
        switch self {
        case .portrait, .portraitUpsideDown, .landscapeLeft, .landscapeRight: return true
        default: return false
        }
    }
}
final class UIDevice {
    static let current = UIDevice()
    var orientation: UIDeviceOrientation = .landscapeLeft
}
struct SensorVector {
    var x: Double = 0
    var y: Double = 0
    var z: Double = 0
}
final class CMDeviceMotion {
    var gravity = SensorVector()
    var userAcceleration = SensorVector()
    var rotationRate = SensorVector()
}
final class GCMotion {
    var hasRotationRate = false
    var sensorsActive = false
    var valueChangedHandler: ((GCMotion) -> Void)?
    var acceleration = SensorVector()
    var rotationRate = SensorVector()
}
final class GCController {
    var motion: GCMotion?
}
final class CMMotionManager {
    var isDeviceMotionAvailable = true
    var isDeviceMotionActive = false
    var deviceMotionUpdateInterval = 0.0
    var handler: ((CMDeviceMotion?, Error?) -> Void)?
    var starts = 0
    func startDeviceMotionUpdates(to queue: OperationQueue, withHandler body: @escaping (CMDeviceMotion?, Error?) -> Void) {
        isDeviceMotionActive = true
        handler = body
        starts += 1
    }
    func stopDeviceMotionUpdates() {
        isDeviceMotionActive = false
        handler = nil
    }
}
final class BaseController {
    var nativeController: GCController?
    var virtual = false
    var usesDeviceHandlers = false
    var isActive = true
    let pointer = UnsafeMutableRawPointer(bitPattern: 1)!
    var orientation = UIDeviceOrientation.landscapeLeft
    let motionOperation = OperationQueue()
    let motionQueue = DispatchQueue(label: "motion-regression")
    let motionManager = CMMotionManager()
    let motionSubmitInterval: UInt64 = 10_000_000
    var lastMotionSubmit: UInt64 = 0
    var motionGeneration: UInt64 = 0
    var motionSource: MotionSource?
    func drain() { motionQueue.sync {} }
}
final class ControllerMappingStore {
    static let shared = ControllerMappingStore()
    var current = ControllerMapping.standard()
    func mapping(for controller: BaseController) -> ControllerMapping { current }
}
enum Ryujinx {
    static var samples: [(Int, SIMD3<Float>)] = []
    static func setGamepadMotion(_ id: UnsafeMutableRawPointer?, motionType: Int, axis: SIMD3<Float>) {
        samples.append((motionType, axis))
    }
}
'''

TESTS = r'''
func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fatalError(message) }
}
func near(_ actual: SIMD3<Float>, _ expected: SIMD3<Float>) -> Bool {
    (0..<3).allSatisfy { abs(actual[$0] - expected[$0]) < 0.001 }
}
let store = ControllerMappingStore.shared
let legacyData = Data(#"{"motionEnabled":false}"#.utf8)
let decoded = try JSONDecoder().decode(ControllerMapping.self, from: legacyData)
require(decoded.deviceMotionMode == .auto && !decoded.motionEnabled, "old JSON migration")
for mode in DeviceMotionMode.allCases {
    var mapping = ControllerMapping.standard()
    mapping.deviceMotionMode = mode
    mapping.motionEnabled = false
    let data = try JSONEncoder().encode(mapping)
    let roundTrip = try JSONDecoder().decode(ControllerMapping.self, from: data)
    require(roundTrip == mapping, "JSON round trip \(mode)")
    let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    require(json["deviceMotionMode"] as? String == mode.rawValue, "stable JSON key/value")
}
print("PASS: old JSON migration and all policy round trips")

var routingCases = 0
for mode in DeviceMotionMode.allCases {
    for virtual in [false, true] {
        for profile in 0...2 { // nil, acceleration-only, gyro-capable
            for enabled in [false, true] {
                for nameFallback in [false, true] {
                    let controller = BaseController()
                    let native = GCController()
                    if profile > 0 { native.motion = GCMotion() }
                    native.motion?.hasRotationRate = profile == 2
                    controller.nativeController = native
                    controller.virtual = virtual
                    controller.usesDeviceHandlers = virtual || nameFallback
                    store.current = .standard()
                    store.current.deviceMotionMode = mode
                    store.current.motionEnabled = enabled
                    controller.setupMotion()
                    let expected: BaseController.MotionSource?
                    if virtual || mode == .always {
                        expected = .device
                    } else if mode == .auto {
                        expected = profile == 2 && enabled ? .controller : .device
                    } else if profile == 0 || nameFallback {
                        expected = .device
                    } else {
                        expected = enabled ? .controller : nil
                    }
                    require(controller.motionSource == expected, "routing \(mode)/\(virtual)/\(profile)/\(enabled)/\(nameFallback)")
                    require(controller.motionManager.isDeviceMotionActive == (expected == .device), "phone sensor activation")
                    require((native.motion?.sensorsActive ?? false) == (expected == .controller), "controller sensor activation")
                    let starts = controller.motionManager.starts
                    controller.setupMotion()
                    require(controller.motionManager.starts == starts, "duplicate start")
                    controller.stopMotion()
                    require(!controller.motionManager.isDeviceMotionActive, "stop phone")
                    require(!(native.motion?.sensorsActive ?? false), "stop controller")
                    routingCases += 1
                }
            }
        }
    }
}
print("PASS: \(routingCases) routing cases, exclusive activation, idempotent starts, stops")

let controller = BaseController()
let native = GCController()
let hardware = GCMotion()
native.motion = hardware
hardware.hasRotationRate = true
controller.nativeController = native
store.current = .standard()
store.current.deviceMotionMode = .always
controller.setupMotion()
require(controller.motionManager.deviceMotionUpdateInterval == 1.0 / 60.0, "60 Hz")
let phoneHandler = controller.motionManager.handler!
let sample = CMDeviceMotion()
sample.gravity = SensorVector(x: 0.25, y: 0.5, z: 0.75)
sample.userAcceleration = SensorVector(x: 0.75, y: 1.5, z: 2.25)
sample.rotationRate = SensorVector(x: .pi, y: 2 * .pi, z: 3 * .pi)
let orientations: [(UIDeviceOrientation, SIMD3<Float>, SIMD3<Float>)] = [
    (.landscapeLeft, SIMD3(-2, 1, -3), SIMD3(-360, -180, -540)),
    (.landscapeRight, SIMD3(2, -1, -3), SIMD3(360, 180, -540)),
    (.faceUp, SIMD3(2, -1, -3), SIMD3(360, 180, -540)),
    (.unknown, SIMD3(2, -1, -3), SIMD3(360, 180, -540))
]
for (orientation, accel, gyro) in orientations {
    UIDevice.current.orientation = orientation
    Ryujinx.samples.removeAll()
    phoneHandler(sample, nil)
    controller.drain()
    require(Ryujinx.samples.count == 2, "two device vectors")
    require(Ryujinx.samples[0].0 == 1 && near(Ryujinx.samples[0].1, accel), "acceleration units/axes")
    require(Ryujinx.samples[1].0 == 2 && near(Ryujinx.samples[1].1, gyro), "gyro units/axes")
}
store.current.deviceMotionMode = .auto
controller.setupMotion()
Ryujinx.samples.removeAll()
phoneHandler(sample, nil)
controller.drain()
require(Ryujinx.samples.isEmpty, "reject old phone callback after switch")
let controllerHandler = hardware.valueChangedHandler!
hardware.acceleration = SensorVector(x: 1, y: 2, z: 3)
hardware.rotationRate = sample.rotationRate
controllerHandler(hardware)
controller.drain()
require(Ryujinx.samples.count == 2, "controller vectors")
require(near(Ryujinx.samples[0].1, SIMD3(-1, -3, 2)), "controller accel axes")
require(near(Ryujinx.samples[1].1, SIMD3(180, 540, -360)), "controller gyro axes")
controller.stopMotion()
require(Ryujinx.samples.suffix(2).allSatisfy { $0.1 == .zero }, "clear retained vectors")
Ryujinx.samples.removeAll()
controllerHandler(hardware)
phoneHandler(sample, nil)
controller.drain()
require(Ryujinx.samples.isEmpty, "reject both old callback types after stop")
controller.setupMotion()
require(hardware.sensorsActive, "resume")
store.current.deviceMotionMode = .always
controller.setupMotion()
controller.motionManager.handler?(nil, NSError(domain: "test", code: 1))
controller.drain()
require(!controller.motionManager.isDeviceMotionActive && controller.motionSource == nil, "stop on sensor error")
controller.motionManager.isDeviceMotionAvailable = false
controller.setupMotion()
require(!controller.motionManager.isDeviceMotionActive, "unavailable device motion")
controller.motionManager.isDeviceMotionAvailable = true
controller.isActive = false
controller.setupMotion()
require(!controller.motionManager.isDeviceMotionActive, "do not restart disconnected controller")
print("PASS: actual Swift conversions, landscape retention, stale callbacks, zeroing, resume/error/unavailable/disconnected")
'''


def swift_harness(repo):
    model = (repo / MODEL).read_text(encoding="utf-8").split("extension VirtualControllerButton", 1)[0]
    motion = (repo / MOTION).read_text(encoding="utf-8")
    clean = lambda text: re.sub(r"^import .+\n", "", text, flags=re.M)
    constants = "\n".join(f'let {name} = "{name}"' for name in sorted(set(re.findall(r"\bGCInput\w+", model))))
    buttons = sorted(set(re.findall(r"VirtualControllerButton\.(\w+)\.id", model)))
    button_stub = "enum VirtualControllerButton: String, CaseIterable {\ncase " + ", ".join(buttons) + r'''
    var id: String { rawValue }
    static func bindable(id: String) -> VirtualControllerButton? { Self(rawValue: id) }
}
'''
    return STUBS + "\n" + constants + "\n" + button_stub + clean(model) + clean(motion) + TESTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=pathlib.Path)
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    static_checks(repo)
    if args.static_only:
        print("SKIPPED: Swift compilation and execution (static-only requested)")
        return
    with tempfile.TemporaryDirectory(prefix="melonx-motion-") as directory:
        source = pathlib.Path(directory) / "main.swift"
        executable = pathlib.Path(directory) / "motion-tests"
        source.write_text(swift_harness(repo), encoding="utf-8")
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", str(source), "-o", str(executable)], check=True)
        subprocess.run([str(executable)], check=True)
    print("PASS: motion/model logic with test doubles; real iOS framework/build/device tests still required")


if __name__ == "__main__":
    main()
