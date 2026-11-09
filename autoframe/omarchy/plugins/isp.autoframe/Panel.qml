import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
// SPDX-License-Identifier: MIT
//
// ISP front camera: automatic framing (isp-autoframe). Bar button + popup:
// follow faces or show the whole field of view, and the settings (framing
// tightness, zoom limit, headroom, speed, hold) as sliders.
//
// State is ~/.config/qualcomm-isp/autoframe.conf (key=value, written by the
// isp-autoframe tool, read by the ispautoframe element in the camera
// publisher within a second). The file is also the saved configuration.
//
//   omarchy-shell isp.autoframe open | close | toggle
Panel {
  id: root
  moduleName: "isp.autoframe"
  ipcTarget: "isp.autoframe"

  readonly property string controlPath: (Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")) + "/qualcomm-isp/autoframe.conf"
  readonly property string tool: "isp-autoframe"

  // Element defaults (gstispautoframe.cpp); shown when the file has no value.
  readonly property var defaults: ({ face: 0.30, zoom: 2.0, headroom: 0.12, speed: 4.0, hold: 3.0 })

  property bool available: false
  property bool following: true
  property real face: defaults.face
  property real zoom: defaults.zoom
  property real headroom: defaults.headroom
  property real speed: defaults.speed
  property real hold: defaults.hold
  property bool boxes: false

  readonly property var presets: [
    { label: "Loose", face: 0.22, detail: "Room around you, more of the desk" },
    { label: "Normal", face: 0.30, detail: "Head and shoulders, like Windows" },
    { label: "Tight", face: 0.40, detail: "Close in on the face" }
  ]

  readonly property bool atDefaults: Math.abs(face - defaults.face) < 0.005 && Math.abs(zoom - defaults.zoom) < 0.05
    && Math.abs(headroom - defaults.headroom) < 0.005 && Math.abs(speed - defaults.speed) < 0.05
    && Math.abs(hold - defaults.hold) < 0.05 && !boxes

  readonly property string barText: following ? "󰄀" : "󰄁"
  readonly property string statusText: {
    if (!available) return "Camera framing not installed"
    if (!following) return "Full field of view"
    return "Following · " + Math.round(face * 100) + "% face · up to " + zoom.toFixed(1) + "×"
  }

  // ---- the tool, one call at a time ----
  property var queue: []
  function run(args) {
    queue = queue.concat([[tool].concat(args)])
    pump()
  }
  function pump() {
    if (proc.running || queue.length === 0) return
    var next = queue[0]
    queue = queue.slice(1)
    proc.command = next
    proc.running = true
  }
  Process {
    id: proc
    onExited: root.pump()
  }

  function setFollowing(on) { following = on; run([on ? "auto" : "off"]) }
  function setFace(v) { face = v; run(["--face", v.toFixed(2)]) }
  function setZoom(v) { zoom = v; run(["--zoom", v.toFixed(1)]) }
  function setHeadroom(v) { headroom = v; run(["--headroom", v.toFixed(2)]) }
  function setSpeed(v) { speed = v; run(["--speed", v.toFixed(1)]) }
  function setHold(v) { hold = v; run(["--hold", v.toFixed(1)]) }
  function setBoxes(on) { boxes = on; run(["--boxes", on ? "on" : "off"]) }
  function resetAll() {
    face = defaults.face; zoom = defaults.zoom; headroom = defaults.headroom
    speed = defaults.speed; hold = defaults.hold; boxes = false
    run(["--face", defaults.face.toFixed(2), "--zoom", defaults.zoom.toFixed(1),
         "--headroom", defaults.headroom.toFixed(2), "--speed", defaults.speed.toFixed(1),
         "--hold", defaults.hold.toFixed(1), "--boxes", "off"])
  }
  function quickToggle() { setFollowing(!following) }

  // ---- state file ----
  function applyState(text) {
    var kv = {}
    var lines = String(text || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var eq = lines[i].indexOf("=")
      if (eq > 0) kv[lines[i].substring(0, eq).trim()] = lines[i].substring(eq + 1).trim()
    }
    following = kv["mode"] !== "off"
    face = num(kv["face-frac"], defaults.face)
    zoom = num(kv["max-zoom"], defaults.zoom)
    headroom = num(kv["headroom"], defaults.headroom)
    speed = num(kv["speed"], defaults.speed)
    hold = num(kv["hold-time"], defaults.hold)
    boxes = kv["draw-boxes"] === "1" || kv["draw-boxes"] === "true"
  }
  function num(s, fallback) {
    var v = parseFloat(s)
    return isNaN(v) ? fallback : v
  }

  FileView {
    path: root.controlPath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { root.available = true; root.applyState(text()) }
    onLoadFailed: root.applyState("")
  }

  // The element exists once qualcomm-isp-autoframe is installed, even before
  // the settings file is first written.
  Process {
    id: dirCheck
    command: ["test", "-f", "/usr/lib/gstreamer-1.0/libgstispautoframe.so"]
    onExited: function(code) { if (code === 0) root.available = true }
  }
  Component.onCompleted: dirCheck.running = true
  onOpenedChanged: if (opened) dirCheck.running = true

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    dimmed: !root.following || !root.available
    fontSize: Style.font.caption
    horizontalMargin: 5
    verticalPadding: 5
    slotSize: Style.bar.statusSlot
    tooltipText: root.opened ? "" : "Camera framing: " + root.statusText + "\nRight-click to switch"
    onPressed: function(b) {
      if (b === Qt.RightButton) root.quickToggle()
      else root.toggle()
    }
  }

  // One labelled slider: title left, live value right, track below.
  component Setting: Column {
    id: setting
    property string title: ""
    property real minimum: 0
    property real maximum: 1
    property real step: 0.01
    property real value: 0
    property var format: function(v) { return v.toFixed(2) }
    property string hint: ""
    signal commit(real value)
    property real live: value
    onValueChanged: live = value

    width: parent.width
    spacing: Style.space(4)

    Item {
      width: parent.width
      implicitHeight: titleText.implicitHeight
      Text {
        id: titleText
        textFormat: Text.PlainText
        text: setting.title
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
        anchors.left: parent.left
      }
      Text {
        textFormat: Text.PlainText
        text: setting.format(setting.live)
        color: root.bar.foreground
        opacity: 0.7
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
        anchors.right: parent.right
      }
    }

    PanelSlider {
      bar: root.bar
      width: parent.width
      minimum: setting.minimum
      maximum: setting.maximum
      step: setting.step
      value: setting.value
      onMoved: function(v) { setting.live = v }
      onReleased: function(v) { setting.live = v; setting.commit(v) }
    }

    Text {
      visible: setting.hint !== ""
      textFormat: Text.PlainText
      width: parent.width
      text: setting.hint
      color: root.bar.foreground
      opacity: 0.5
      wrapMode: Text.WordWrap
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.quickToggle()

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: root.barText
            color: root.bar.foreground
            opacity: root.following ? 1 : 0.6
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "Camera framing"
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.statusText.toUpperCase()
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- Mode ----------
        Row {
          id: modeRow
          width: parent.width
          spacing: Style.space(6)
          readonly property real cellWidth: (width - spacing) / 2

          Button {
            width: modeRow.cellWidth
            iconText: "󰄀"
            text: "Follow me"
            fontSize: Style.font.body
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
            bordered: true
            active: root.following
            enabled: root.available
            onClicked: root.setFollowing(true)
          }
          Button {
            width: modeRow.cellWidth
            iconText: "󰄁"
            text: "Full view"
            fontSize: Style.font.body
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            verticalPadding: Style.spacing.controlPaddingY + Style.space(2)
            bordered: true
            active: !root.following
            enabled: root.available
            onClicked: root.setFollowing(false)
          }
        }

        // ---------- Framing ----------
        Column {
          width: parent.width
          spacing: Style.space(10)
          opacity: root.following ? 1 : 0.5

          PanelSectionHeader {
            text: "FRAMING"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Row {
            id: presetRow
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * (root.presets.length - 1)) / root.presets.length

            Repeater {
              model: root.presets
              Button {
                required property var modelData
                width: presetRow.cellWidth
                text: modelData.label
                tooltipText: modelData.detail
                fontSize: Style.font.bodySmall
                foreground: root.bar.foreground
                fontFamily: root.bar.fontFamily
                bordered: true
                active: Math.abs(root.face - modelData.face) < 0.015
                onClicked: root.setFace(modelData.face)
              }
            }
          }

          Setting {
            title: "Face size"
            minimum: 0.15; maximum: 0.50; step: 0.01
            value: root.face
            format: function(v) { return Math.round(v * 100) + "% of the frame" }
            onCommit: function(v) { root.setFace(v) }
          }

          Setting {
            title: "Zoom limit"
            minimum: 1.0; maximum: 4.0; step: 0.1
            value: root.zoom
            format: function(v) { return v.toFixed(1) + "×" + (v > 2.05 ? "  (softer past 2×)" : "") }
            hint: "Up to 2× stays native 1080p; beyond that the crop is upscaled."
            onCommit: function(v) { root.setZoom(v) }
          }

          Setting {
            title: "Headroom"
            minimum: -0.10; maximum: 0.30; step: 0.01
            value: root.headroom
            format: function(v) { return (v >= 0 ? "+" : "") + Math.round(v * 100) + "%" }
            onCommit: function(v) { root.setHeadroom(v) }
          }
        }

        // ---------- Motion ----------
        Column {
          width: parent.width
          spacing: Style.space(10)
          opacity: root.following ? 1 : 0.5

          PanelSectionHeader {
            text: "MOTION"
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
          }

          Setting {
            title: "Speed"
            minimum: 1.0; maximum: 10.0; step: 0.5
            value: root.speed
            format: function(v) { return v <= 2.5 ? "lazy" : v <= 5.5 ? "smooth" : v <= 8 ? "quick" : "snappy" }
            onCommit: function(v) { root.setSpeed(v) }
          }

          Setting {
            title: "Hold after you leave"
            minimum: 0; maximum: 10; step: 0.5
            value: root.hold
            format: function(v) { return v.toFixed(1) + " s" }
            onCommit: function(v) { root.setHold(v) }
          }
        }

        PanelSeparator { foreground: root.bar.foreground }

        // ---------- Footer ----------
        Column {
          width: parent.width
          spacing: Style.space(6)

          Toggle {
            width: parent.width
            label: "Show detection boxes"
            description: "Draws the face detector's boxes on the video (for tuning)"
            checked: root.boxes
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            onClicked: root.setBoxes(!root.boxes)
          }

          Button {
            width: parent.width
            text: "Reset to defaults"
            fontSize: Style.font.bodySmall
            foreground: root.bar.foreground
            fontFamily: root.bar.fontFamily
            bordered: true
            enabled: !root.atDefaults
            opacity: root.atDefaults ? 0.5 : 1
            onClicked: root.resetAll()
          }
        }
      }
    }
  }
}
