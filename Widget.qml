import QtQuick
import Quickshell
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "io.github.ctl0v0.omasonos"

  readonly property var sonos: bar?.shell?.serviceFor(moduleName)
  readonly property var playback: sonos ? sonos.playback : ({})
  readonly property var target: sonos ? sonos.target : null
  readonly property bool online: sonos && sonos.ready && target !== null
  readonly property bool playing: String(playback.state || "").toUpperCase() === "PLAYING"
  readonly property string title: String(playback.title || "")
  readonly property string artist: String(playback.artist || "")
  readonly property string roomLabel: target ? String(target.roomLabel || "Sonos") : "Sonos"
  readonly property var actions: playback.availableActions || []
  readonly property var activeHousehold: {
    if (!target) return null
    for (var i = 0; i < sonos.households.length; i++) {
      if (sonos.households[i].id === target.householdId) return sonos.households[i]
    }
    return null
  }
  readonly property var groups: activeHousehold ? activeHousehold.groups : []
  readonly property var allGroups: {
    var out = []
    if (!sonos) return out
    for (var h = 0; h < sonos.households.length; h++) {
      var household = sonos.households[h]
      for (var g = 0; g < household.groups.length; g++) {
        var entry = household.groups[g]
        out.push({
          uid: entry.uid,
          label: entry.label,
          householdId: household.id,
          playbackState: entry.playbackState
        })
      }
    }
    return out
  }
  readonly property var rooms: activeHousehold ? activeHousehold.rooms : []
  property bool popupOpen: false
  property real maxLabelWidth: 190
  property var stagedRoomUids: []
  property bool groupingDirty: false
  property bool groupingApplying: false

  function hasAction(name) { return actions.indexOf(name) !== -1 }
  function close() { popupOpen = false }
  function resetStagedRooms() {
    stagedRoomUids = target && target.memberUids ? target.memberUids.slice() : []
    groupingDirty = false
  }
  function roomStaged(uid) { return stagedRoomUids.indexOf(uid) !== -1 }
  function toggleStagedRoom(uid) {
    var next = stagedRoomUids.slice()
    var index = next.indexOf(uid)
    if (index === -1) next.push(uid)
    else if (next.length > 1) next.splice(index, 1)
    stagedRoomUids = next
    groupingDirty = true
  }
  function stageEverywhere() {
    var next = []
    for (var i = 0; i < rooms.length; i++) next.push(rooms[i].uid)
    stagedRoomUids = next
    groupingDirty = true
  }
  function applyStagedRooms() {
    if (!sonos || stagedRoomUids.length === 0) return
    groupingApplying = true
    sonos.applyMembers(stagedRoomUids)
  }
  function formatTime(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(Number(seconds))) return "--:--"
    var total = Math.max(0, Math.floor(Number(seconds)))
    var hours = Math.floor(total / 3600)
    var minutes = Math.floor((total % 3600) / 60)
    var secs = total % 60
    var mm = (minutes < 10 && hours > 0 ? "0" : "") + minutes
    var ss = (secs < 10 ? "0" : "") + secs
    return hours > 0 ? hours + ":" + mm + ":" + ss : minutes + ":" + ss
  }

  onTargetChanged: {
    var finishedApply = groupingApplying
    groupingApplying = false
    if (!groupingDirty || finishedApply) resetStagedRooms()
  }

  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize
  opacity: online ? 1.0 : 0.52

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      id: glyph
      anchors.verticalCenter: parent.verticalCenter
      text: root.playing ? "󰓃" : "󰓄"
      color: root.bar.barForeground
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

    Item {
      id: scrollClip
      width: Math.min(root.maxLabelWidth, labelText.implicitWidth)
      height: glyph.height
      clip: true
      anchors.verticalCenter: parent.verticalCenter
      visible: !root.bar.vertical

      Text {
        id: labelText
        text: root.online
          ? (root.title || root.roomLabel) + (root.artist ? "  ·  " + root.artist : "")
          : "Sonos offline"
        color: root.bar.barForeground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter

        readonly property bool needsScroll: implicitWidth > scrollClip.width
        NumberAnimation on x {
          running: labelText.needsScroll && !root.popupOpen && !root.bar.vertical
          loops: Animation.Infinite
          duration: Math.max(6500, labelText.implicitWidth * 28)
          from: scrollClip.width
          to: -labelText.implicitWidth
          easing.type: Easing.Linear
        }
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton
    cursorShape: Qt.PointingHandCursor

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) {
        if (root.online) root.sonos.playPause()
      } else {
        root.popupOpen = !root.popupOpen
      }
    }

    onWheel: function(wheel) {
      if (!root.online) return
      root.sonos.adjustGroupVolume(wheel.angleDelta.y > 0 ? 5 : -5)
    }

    onEntered: if (root.bar) root.bar.showTooltip(
      root,
      root.online
        ? (root.roomLabel + " · " + (root.title || (root.playing ? "Playing" : "Paused")))
        : (root.sonos && root.sonos.snapshot.status ? root.sonos.snapshot.status.message : "Sonos offline")
    )
    onExited: if (root.bar) root.bar.hideTooltip(root)
  }

  onPopupOpenChanged: if (sonos) sonos.setPanelOpen(popupOpen)

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(360))
    contentHeight: popup.fittedContentHeight(content.implicitHeight)

    Column {
      id: content
      anchors.fill: parent
      spacing: Style.spacing.panelGap

      Row {
        width: parent.width
        spacing: Style.spacing.panelGap

        BorderSurface {
          width: Style.space(68)
          height: Style.space(68)
          radius: Style.spacing.labelGap
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

          Image {
            anchors.fill: parent
            anchors.margins: Style.space(2)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            source: root.playback.artworkUrl || ""
            visible: source !== ""
          }

          Text {
            anchors.centerIn: parent
            text: "󰓃"
            visible: !root.playback.artworkUrl
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge
          }
        }

        Column {
          width: parent.width - Style.space(80)
          spacing: Style.space(3)

          Text {
            width: parent.width
            text: root.online ? (root.title || "Nothing playing") : "Sonos unavailable"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }
          Text {
            width: parent.width
            text: root.artist
            visible: text !== ""
            color: Qt.darker(root.bar.foreground, 1.35)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
          Text {
            width: parent.width
            text: root.roomLabel + (root.playback.source ? " · " + root.playback.source : "")
            color: Qt.darker(root.bar.foreground, 1.55)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(3)
        visible: root.online && root.hasAction("SeekTime")
          && Number(root.playback.durationSec || 0) > 0

        PanelSlider {
          bar: root.bar
          width: parent.width
          minimum: 0
          maximum: Math.max(1, Number(root.playback.durationSec || 1))
          step: 1
          value: Number(root.playback.positionSec || 0)
          onMoved: function(v) { root.sonos.seek(v) }
        }

        Row {
          width: parent.width
          Text {
            text: root.formatTime(root.playback.positionSec)
            color: Qt.darker(root.bar.foreground, 1.45)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
          Item { width: parent.width - parent.children[0].width - parent.children[2].width }
          Text {
            text: root.formatTime(root.playback.durationSec)
            color: Qt.darker(root.bar.foreground, 1.45)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(6)

        Button {
          iconText: "󰒮"
          foreground: root.bar.foreground
          enabled: root.online && root.hasAction("Previous")
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.previous()
        }
        Button {
          iconText: root.playing ? "󰏤" : "󰐊"
          foreground: root.bar.foreground
          iconSize: Style.font.iconLarge
          enabled: root.online && (root.hasAction("Play") || root.hasAction("Pause"))
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.playPause()
        }
        Button {
          iconText: "󰒭"
          foreground: root.bar.foreground
          enabled: root.online && root.hasAction("Next")
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.next()
        }
      }

      PanelSeparator { foreground: root.bar.foreground }

      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: root.target && root.target.mute ? "󰖁" : "󰕾"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.icon
        }

        PanelSlider {
          id: groupVolume
          bar: root.bar
          width: parent.width - Style.space(66)
          minimum: 0
          maximum: 100
          step: 5
          value: root.target ? Number(root.target.volume || 0) : 0
          enabled: root.online
          onMoved: function(v) { root.sonos.setGroupVolume(v) }
        }

        Button {
          iconText: root.target && root.target.mute ? "󰝟" : "󰓄"
          foreground: root.bar.foreground
          enabled: root.online
          onClicked: root.sonos.setGroupMute(!(root.target && root.target.mute))
        }
      }

      Text {
        width: parent.width
        visible: !root.online
        text: root.sonos && root.sonos.lastError
          ? root.sonos.lastError
          : (root.sonos && root.sonos.snapshot.status ? root.sonos.snapshot.status.message : "Looking for Sonos…")
        color: Color.urgent
        wrapMode: Text.Wrap
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Column {
        width: parent.width
        spacing: Style.space(5)
        visible: root.online && root.allGroups.length > 1

        Text {
          text: "Groups"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
        }

        Repeater {
          model: root.allGroups
          Button {
            required property var modelData
            width: parent.width
            text: modelData.label + (root.sonos.households.length > 1
              ? "  ·  " + modelData.householdId : "")
            foreground: root.bar.foreground
            bordered: true
            active: root.target && modelData.uid === root.target.groupUid
            onClicked: root.sonos.selectGroup(modelData.uid)
          }
        }
      }

      PanelSeparator {
        visible: root.online && root.rooms.length > 0
        foreground: root.bar.foreground
      }

      Column {
        id: roomMixer
        width: parent.width
        spacing: Style.space(5)
        visible: root.online && root.rooms.length > 0

        Text {
          text: "Rooms"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
        }

        Repeater {
          model: root.rooms
          Row {
            id: roomRow
            required property var modelData
            width: roomMixer.width
            spacing: Style.space(6)

            Text {
              width: Style.space(92)
              anchors.verticalCenter: parent.verticalCenter
              text: roomRow.modelData.name
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideRight
            }

            PanelSlider {
              bar: root.bar
              width: roomMixer.width - Style.space(142)
              minimum: 0
              maximum: 100
              step: 5
              value: Number(roomRow.modelData.volume || 0)
              onMoved: function(v) { root.sonos.setRoomVolume(roomRow.modelData.uid, v) }
            }

            Button {
              iconText: roomRow.modelData.mute ? "󰝟" : "󰓄"
              foreground: root.bar.foreground
              onClicked: root.sonos.setRoomMute(roomRow.modelData.uid, !roomRow.modelData.mute)
            }
          }
        }

        Text {
          text: "Group rooms"
          color: Qt.darker(root.bar.foreground, 1.35)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        Flow {
          width: parent.width
          spacing: Style.space(5)

          Repeater {
            model: root.rooms
            Button {
              required property var modelData
              text: modelData.name
              foreground: root.bar.foreground
              bordered: true
              active: root.roomStaged(modelData.uid)
              onClicked: root.toggleStagedRoom(modelData.uid)
            }
          }
        }

        Row {
          width: parent.width
          spacing: Style.space(6)

          Button {
            text: "Everywhere"
            foreground: root.bar.foreground
            bordered: true
            enabled: !root.groupingApplying
            onClicked: root.stageEverywhere()
          }
          Button {
            text: "Cancel"
            foreground: root.bar.foreground
            bordered: true
            enabled: root.groupingDirty && !root.groupingApplying
            onClicked: root.resetStagedRooms()
          }
          Button {
            text: root.groupingApplying ? "Applying…" : "Apply"
            foreground: root.bar.foreground
            bordered: true
            active: root.groupingDirty
            enabled: root.groupingDirty && root.stagedRoomUids.length > 0 && !root.groupingApplying
            onClicked: root.applyStagedRooms()
          }
        }
      }
    }
  }
}
