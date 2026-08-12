#!/usr/bin/env python3
"""Static Android namespace, manifest-component, and applicationId contract."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_PACKAGE = "com.carriez.flutter_hbb"
ANDROID_NAME = "{http://schemas.android.com/apk/res/android}name"


def kotlin_classes() -> set[str]:
    classes: set[str] = set()
    for source in (ROOT / "flutter/android/app/src/main/kotlin").rglob("*.kt"):
        package_match = re.search(r"^package\s+([\w.]+)", source.read_text(), re.MULTILINE)
        if not package_match:
            continue
        package = package_match.group(1)
        for match in re.finditer(r"\b(?:class|object|interface)\s+(\w+)", source.read_text()):
            classes.add(f"{package}.{match.group(1)}")
    return classes


def main() -> None:
    gradle = (ROOT / "flutter/android/app/build.gradle").read_text()
    if 'namespace "com.carriez.flutter_hbb"' not in gradle:
        raise AssertionError("Android namespace must remain bound to the Kotlin package")
    if 'applicationId "com.carriez.flutter_hbb"' not in gradle:
        raise AssertionError("Android Gradle applicationId marker is missing")

    workflow = (ROOT / ".github/workflows/rustqs-android.yml").read_text()
    if "manifest.write_text" in workflow or 'package=\\"$RQS_ANDROID_APP_ID\\"' in workflow:
        raise AssertionError("Android workflow must not rewrite the manifest package")
    for marker in (
        'gradle.write_text(gradle_text.replace(\'applicationId "com.carriez.flutter_hbb"\'',
        'grep -F -q -- \'package="com.carriez.flutter_hbb"\'',
    ):
        if marker not in workflow:
            raise AssertionError(f"Android workflow contract is missing {marker!r}")

    classes = kotlin_classes()
    main_manifest = ROOT / "flutter/android/app/src/main/AndroidManifest.xml"
    if 'android:label="@string/app_name"' not in main_manifest.read_text():
        raise AssertionError("Android manifest must use the authored app_name resource")
    for manifest_path in (
        main_manifest,
        ROOT / "flutter/android/app/src/debug/AndroidManifest.xml",
        ROOT / "flutter/android/app/src/profile/AndroidManifest.xml",
    ):
        root = ET.parse(manifest_path).getroot()
        if root.attrib.get("package") != BASE_PACKAGE:
            raise AssertionError(f"{manifest_path}: manifest namespace changed unexpectedly")
        for element in root.iter():
            name = element.attrib.get(ANDROID_NAME)
            if not name or not name.startswith("."):
                continue
            resolved = f"{BASE_PACKAGE}{name}"
            if resolved not in classes:
                raise AssertionError(f"{manifest_path}: component {name!r} resolves to missing {resolved}")


if __name__ == "__main__":
    main()
