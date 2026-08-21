import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Ui
import qs.Commons

BarWidget {
  id: root
  moduleName: "io.github.ctl0v0.omasonos"

  readonly property var sonos: bar?.shell?.serviceFor(moduleName)
  readonly property var serviceSnapshot: sonos && sonos.snapshot ? sonos.snapshot : null
  readonly property var playback: serviceSnapshot && serviceSnapshot.playback
    ? serviceSnapshot.playback : ({})
  readonly property var target: serviceSnapshot ? serviceSnapshot.target : null
  readonly property string selectedRoomUid: serviceSnapshot
    ? String(serviceSnapshot.selectedAnchorRoomUid || "") : ""
  readonly property var favorites: serviceSnapshot && serviceSnapshot.favorites
    ? serviceSnapshot.favorites : ({ state: "not_loaded", items: [], total: 0, unsupported: 0, error: "" })
  readonly property bool online: !!sonos && sonos.ready && target !== null
  readonly property string connectionState: serviceSnapshot && serviceSnapshot.status
    ? String(serviceSnapshot.status.state || "starting") : "starting"
  readonly property var connectionStatus: serviceSnapshot && serviceSnapshot.status
    ? serviceSnapshot.status : ({})
  readonly property bool degraded: connectionStatus.degraded === true
    || playback.stale === true
  readonly property string statusMessage: String(connectionStatus.message || "")
  readonly property bool connectionProblem: connectionState === "error"
    || connectionState === "setup_error"
  readonly property string disconnectedTitle: connectionProblem
    ? "Sonos needs attention"
    : (connectionState === "starting" || connectionState === "discovering"
      ? "Looking for Sonos"
      : "Away from Sonos")
  readonly property string disconnectedDetail: connectionProblem
    ? (sonos && sonos.lastError
      ? sonos.lastError
      : (serviceSnapshot && serviceSnapshot.status
        ? String(serviceSnapshot.status.message || "OmaSonos could not start.")
        : "OmaSonos could not start."))
    : (connectionState === "starting" || connectionState === "discovering"
      ? "Checking this network for your speakers…"
      : "You’re not on your Sonos network. OmaSonos will reconnect automatically when you’re back on the same Wi-Fi.")
  readonly property bool favoriteStarting: !!sonos
    && (sonos.favoriteRequestId !== "" || sonos.favoriteAwaitingSnapshot)
  readonly property bool movePending: !!sonos && sonos.moveRequestId !== ""
  readonly property string favoriteStartingTitle: sonos
    ? String(sonos.favoriteStartingTitle || "") : ""
  readonly property string playbackState: String(playback.state || "").toUpperCase()
  readonly property bool playing: playbackState === "PLAYING"
    || playbackState === "TRANSITIONING"
  readonly property string title: String(playback.title || "")
  readonly property string artist: String(playback.artist || "")
  readonly property string displayTitle: {
    if (title !== "") return title
    if (playing) {
      return String(playback.source || "").toUpperCase() === "RADIO"
        ? "Radio playing"
        : "Audio playing"
    }
    if (playbackState === "PAUSED_PLAYBACK") return "Audio paused"
    return "Nothing playing"
  }
  readonly property string roomLabel: target ? String(target.roomLabel || "Sonos") : "Sonos"
  readonly property string barLabel: {
    if (!online) return disconnectedTitle
    if (title !== "") {
      return roomLabel + " · " + title + (artist !== "" ? " — " + artist : "")
    }
    if (playbackState === "PAUSED_PLAYBACK") return roomLabel + " · Paused"
    return roomLabel
  }
  readonly property var actions: playback.availableActions || []
  readonly property var activeHousehold: {
    if (!target) return null
    var households = serviceSnapshot && serviceSnapshot.households
      ? serviceSnapshot.households : []
    for (var i = 0; i < households.length; i++) {
      if (households[i].id === target.householdId) return households[i]
    }
    return null
  }
  readonly property var groups: activeHousehold ? activeHousehold.groups : []
  readonly property var multiRoomGroups: {
    var out = []
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].memberUids && groups[i].memberUids.length > 1) out.push(groups[i])
    }
    return out
  }
  readonly property var allGroups: {
    var out = []
    var households = serviceSnapshot && serviceSnapshot.households
      ? serviceSnapshot.households : []
    for (var h = 0; h < households.length; h++) {
      var household = households[h]
      for (var g = 0; g < household.groups.length; g++) {
        var entry = household.groups[g]
        out.push({
          uid: entry.uid,
          label: entry.label,
          householdId: household.id,
          playbackState: entry.playbackState,
          volume: entry.volume,
          mute: entry.mute
        })
      }
    }
    return out
  }
  readonly property var rooms: activeHousehold ? activeHousehold.rooms : []
  property bool popupOpen: false
  readonly property real maxLabelWidth: Math.max(80, Number(setting("maxLabelWidth", 220)))
  property var stagedRoomUids: []
  property bool groupingDirty: false
  property bool groupingApplying: false
  property bool groupSettingsOpen: false
  property bool locationPickerOpen: false
  property bool playbackSessionsOpen: false
  property bool favoritesOpen: false
  property bool popoutSwitchClosing: false
  property var focusedControl: null
  readonly property bool opened: popupOpen

  function hasAction(name) { return actions.indexOf(name) !== -1 }
  function open() { popupOpen = true }
  function close() { popupOpen = false }
  function toggle() { popupOpen = !popupOpen }
  function closeForPopoutSwitch() {
    popoutSwitchClosing = true
    close()
    popoutSwitchTimer.restart()
  }
  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(root, direction)
    return false
  }
  function safeArtworkUrl(url) {
    var value = String(url || "").trim()
    return value.indexOf("http://") === 0 || value.indexOf("https://") === 0
      ? value : ""
  }
  function activeControl() {
    var window = keyCatcher.QsWindow.window
    var item = window ? window.activeFocusItem : null
    return item && item.focusable === true ? item : focusedControl
  }
  function moveControlFocus(forward) {
    var current = activeControl() || keyCatcher
    var next = current
    for (var i = 0; i < 100; i++) {
      next = next.nextItemInFocusChain(forward)
      if (!next || next === current) return
      if (next.focusable === true && next.enabled && next.visible) {
        focusedControl = next
        next.forceActiveFocus()
        var point = next.mapToItem(content, 0, 0)
        if (point.y < panelScroll.contentY)
          panelScroll.contentY = Math.max(0, point.y - Style.spacing.panelGap)
        else if (point.y + next.height > panelScroll.contentY + panelScroll.height)
          panelScroll.contentY = Math.min(
            Math.max(0, panelScroll.contentHeight - panelScroll.height),
            point.y + next.height - panelScroll.height + Style.spacing.panelGap)
        return
      }
    }
  }
  function activateFocusedControl() {
    var current = activeControl()
    if (current && current.enabled && current.visible && "clicked" in current) {
      current.clicked()
      return
    }
    if (online && (hasAction("Play") || hasAction("Pause"))) sonos.playPause()
  }
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
  function movePlaybackTo(roomUid) {
    if (!sonos || !roomUid) return
    sonos.movePlaybackToRoom(roomUid)
  }
  function roomMoveBlocked(uid) {
    if (!playing) return false
    if (target && target.memberUids && target.memberUids.length > 1) return true
    for (var i = 0; i < groups.length; i++) {
      var group = groups[i]
      if (!group.memberUids || group.memberUids.length < 2) continue
      if (group.memberUids.indexOf(uid) === -1) continue
      return true
    }
    return false
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
  onMovePendingChanged: {
    if (!movePending && sonos && sonos.moveError === "") locationPickerOpen = false
  }

  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize
  opacity: online ? 1.0 : 0.68

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

      Row {
        id: marqueeRow
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(18)

        Text {
          id: labelText
          text: root.barLabel
          color: root.bar.barForeground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          text: root.barLabel
          visible: labelText.implicitWidth > scrollClip.width
          color: root.bar.barForeground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        NumberAnimation on x {
          id: marquee
          running: labelText.implicitWidth > scrollClip.width
            && !root.popupOpen && !root.bar.vertical
          loops: Animation.Infinite
          from: 0
          to: -(labelText.implicitWidth + marqueeRow.spacing)
          duration: Math.max(4000,
            (labelText.implicitWidth + marqueeRow.spacing) * 32)
          easing.type: Easing.Linear
          onRunningChanged: if (!running) marqueeRow.x = 0
        }
      }
    }
  }

  WidgetButton {
    id: barButton
    anchors.fill: parent
    bar: root.bar
    text: " "
    labelVisible: false
    tooltipText: root.online
      ? (root.roomLabel + " · " + (root.title || (root.playing ? "Playing" : "Paused")))
      : root.disconnectedDetail

    onPressed: function(button) {
      if (button === Qt.MiddleButton) {
        if (root.online) root.sonos.playPause()
      } else if (button === Qt.LeftButton) {
        root.toggle()
      }
    }

    onWheelMoved: function(delta) {
      if (!root.online) return
      root.sonos.adjustGroupVolume(delta > 0 ? 5 : -5)
    }
  }

  onPopupOpenChanged: {
    focusedControl = null
    if (sonos) sonos.setPanelOpen(popupOpen)
  }
  Component.onDestruction: if (popupOpen && sonos) sonos.setPanelOpen(false)

  Timer {
    id: popoutSwitchTimer
    interval: 1
    onTriggered: root.popoutSwitchClosing = false
  }

  KeyboardPanel {
    id: popup
    anchorItem: barButton
    bar: root.bar
    owner: root
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(360))
    contentHeight: popup.fittedContentHeight(content.implicitHeight, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveControlFocus(dy > 0)
        else if (dx !== 0 && root.online) root.sonos.adjustGroupVolume(dx * 5)
      }
      onActivateRequested: root.activateFocusedControl()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        var key = String(text).toLowerCase()
        if (!root.online) return
        if (key === "n" && root.hasAction("Next")) root.sonos.next()
        else if (key === "p" && root.hasAction("Previous")) root.sonos.previous()
        else if (key === "m" && root.target) root.sonos.setGroupMute(!root.target.mute)
        else if (key === "g") root.playbackSessionsOpen = !root.playbackSessionsOpen
        else if (key === "r") root.groupSettingsOpen = !root.groupSettingsOpen
      }

      Flickable {
        id: panelScroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar {
          policy: content.implicitHeight > panelScroll.height
            ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
        }

        Column {
          id: content
          width: panelScroll.width
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
            id: artworkImage
            anchors.fill: parent
            anchors.margins: Style.space(2)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            source: root.safeArtworkUrl(root.playback.artworkUrl)
            visible: status === Image.Ready
          }

          Text {
            anchors.centerIn: parent
            text: "♪"
            visible: artworkImage.status !== Image.Ready
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge * 1.35
          }
        }

        Column {
          width: parent.width - Style.space(80)
          spacing: Style.space(3)

          Text {
            width: parent.width
            text: root.online ? root.displayTitle : root.disconnectedTitle
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
            text: root.online
              ? root.roomLabel + (root.playback.source ? " · " + root.playback.source : "")
              : "Local network connection"
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
          id: seekProgress
          bar: root.bar
          width: parent.width
          minimum: 0
          maximum: Math.max(1, Number(root.playback.durationSec || 1))
          step: 1
          value: Number(root.playback.positionSec || 0)
          onMoved: function(v) { root.sonos.seek(v) }
        }

        Item {
          width: parent.width
          implicitHeight: Math.max(elapsedTime.implicitHeight, durationTime.implicitHeight)

          Text {
            id: elapsedTime
            anchors.left: parent.left
            text: root.formatTime(root.playback.positionSec)
            color: Qt.darker(root.bar.foreground, 1.45)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            id: durationTime
            anchors.right: parent.right
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
        visible: root.online

        Button {
          iconText: "󰒮"
          focusable: true
          foreground: root.bar.foreground
          iconSize: Style.font.display
          enabled: root.online && root.hasAction("Previous")
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.previous()
        }
        Button {
          iconText: root.playing ? "󰏤" : "󰐊"
          focusable: true
          foreground: root.bar.foreground
          iconSize: Style.font.displayLarge
          enabled: root.online && (root.hasAction("Play") || root.hasAction("Pause"))
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.playPause()
        }
        Button {
          iconText: "󰒭"
          focusable: true
          foreground: root.bar.foreground
          iconSize: Style.font.display
          enabled: root.online && root.hasAction("Next")
          opacity: enabled ? 1.0 : 0.4
          onClicked: root.sonos.next()
        }
      }

      PanelSeparator {
        visible: root.online
        foreground: root.bar.foreground
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.online

        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: root.target && root.target.mute ? "󰖁" : "󰕾"
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.icon
        }

        PanelSlider {
          id: groupVolume
          property real sentValue: Number.NaN
          bar: root.bar
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - Style.space(66)
          minimum: 0
          maximum: 100
          step: 5
          value: root.target ? Number(root.target.volume || 0) : 0
          enabled: root.online
          onMoved: function(v) {
            if (isNaN(groupVolume.sentValue)) {
              groupVolume.sentValue = v
              root.sonos.setGroupVolume(v)
            }
          }
          onReleased: function(v) {
            if (isNaN(groupVolume.sentValue) || Math.abs(groupVolume.sentValue - v) >= 1)
              root.sonos.setGroupVolume(v)
            groupVolume.sentValue = Number.NaN
          }
        }

        Button {
          iconText: root.target && root.target.mute ? "󰝟" : "󰓄"
          focusable: true
          foreground: root.bar.foreground
          enabled: root.online
          onClicked: root.sonos.setGroupMute(!(root.target && root.target.mute))
        }
      }

      Column {
        width: parent.width
        visible: !root.online
        spacing: Style.space(8)

        Text {
          width: parent.width
          text: root.disconnectedDetail
          color: root.connectionProblem ? Color.urgent : Qt.darker(root.bar.foreground, 1.35)
          wrapMode: Text.Wrap
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Button {
          text: root.connectionState === "discovering" ? "Checking…" : "Check again"
          focusable: true
          foreground: root.bar.foreground
          bordered: true
          enabled: !!root.sonos && root.connectionState !== "discovering"
          onClicked: root.sonos.refresh()
        }
      }

      Text {
        width: parent.width
        visible: root.online && root.degraded
        text: root.statusMessage !== ""
          ? "Using cached Sonos state: " + root.statusMessage
          : "Using cached Sonos state while the network reconnects."
        color: Color.urgent
        wrapMode: Text.Wrap
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        width: parent.width
        visible: root.online && !!root.sonos && root.sonos.lastError
          && root.sonos.favoriteError === "" && root.sonos.moveError === ""
        text: root.sonos ? root.sonos.lastError : ""
        color: Color.urgent
        wrapMode: Text.Wrap
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Column {
        width: parent.width
        spacing: Style.space(5)
        visible: root.online

        Button {
          width: parent.width
          focusable: true
          text: root.movePending
            ? (root.playing ? "Moving audio…" : "Changing room…")
            : (root.playing ? "Playing on: " : "Active room: ") + root.roomLabel
          iconText: root.movePending ? "󰑓" : "󰓃"
          foreground: root.bar.foreground
          bordered: true
          leftAlign: true
          active: root.locationPickerOpen
          enabled: !root.movePending
          tooltipText: root.playing
            ? "Choose where the current audio plays"
            : "Choose which room to control"
          onClicked: root.locationPickerOpen = !root.locationPickerOpen
        }

        Column {
          width: parent.width
          spacing: Style.space(5)
          visible: root.locationPickerOpen

          Text {
            text: root.playing
              ? "Move current audio to a room"
              : "Choose a room to control"
            color: Qt.darker(root.bar.foreground, 1.35)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            visible: root.playing && root.target && root.target.memberUids.length > 1
            text: "This audio is currently grouped. Use Group settings to change membership first."
            color: root.bar.foreground
            wrapMode: Text.Wrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Flow {
            width: parent.width
            spacing: Style.space(5)

            Repeater {
              model: root.rooms
              Button {
                required property var modelData
                focusable: true
                readonly property bool isCurrent: root.playing
                  ? root.target && root.target.memberUids.length === 1
                    && root.target.memberUids[0] === modelData.uid
                  : root.selectedRoomUid === modelData.uid
                readonly property bool moveBlocked: root.roomMoveBlocked(modelData.uid)
                text: modelData.name
                foreground: root.bar.foreground
                bordered: true
                active: isCurrent
                enabled: !root.movePending && !isCurrent && !moveBlocked
                tooltipText: isCurrent
                  ? (root.playing ? "Current playback location" : "Active room")
                  : moveBlocked
                    ? "This room is in another group; change it in Group settings"
                    : (root.playing ? "Move current audio here" : "Control this room")
                onClicked: root.movePlaybackTo(modelData.uid)
              }
            }
          }

          Text {
            width: parent.width
            visible: !!root.sonos && root.sonos.moveError !== ""
            text: root.sonos ? root.sonos.moveError : ""
            color: root.bar.foreground
            wrapMode: Text.Wrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(5)
        visible: root.online && root.allGroups.length > 1

        Button {
          width: parent.width
          focusable: true
          text: root.playbackSessionsOpen ? "Hide other playback sessions" : "Control different audio"
          iconText: "󰓃"
          foreground: root.bar.foreground
          bordered: true
          leftAlign: true
          active: root.playbackSessionsOpen
          tooltipText: "Switch controls without moving audio"
          onClicked: root.playbackSessionsOpen = !root.playbackSessionsOpen
        }

        Column {
          width: parent.width
          spacing: Style.space(5)
          visible: root.playbackSessionsOpen

          Text {
            text: "This switches the controls; it does not move audio."
            color: Qt.darker(root.bar.foreground, 1.35)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Repeater {
            model: root.allGroups
            Button {
              required property var modelData
              focusable: true
              width: parent.width
              text: modelData.label + (root.serviceSnapshot.households.length > 1
                ? "  ·  " + modelData.householdId : "")
              foreground: root.bar.foreground
              bordered: true
              active: root.target && modelData.uid === root.target.groupUid
              onClicked: {
                root.sonos.selectGroup(modelData.uid)
                root.playbackSessionsOpen = false
              }
            }
          }
        }
      }

      Column {
        width: parent.width
        spacing: Style.space(5)
        visible: root.online

        Row {
          width: parent.width
          spacing: Style.space(6)

          Button {
            width: parent.width - refreshFavoritesButton.width - parent.spacing
            focusable: true
            text: root.favoritesOpen
              ? "Hide Favorites"
              : "Favorites" + (root.favorites.items.length > 0
                ? "  ·  " + root.favorites.items.length : "")
            iconText: "󰓎"
            foreground: root.bar.foreground
            bordered: true
            leftAlign: true
            active: root.favoritesOpen
            onClicked: root.favoritesOpen = !root.favoritesOpen
          }

          Button {
            id: refreshFavoritesButton
            focusable: true
            iconText: "󰑐"
            tooltipText: "Refresh Sonos Favorites"
            foreground: root.bar.foreground
            onClicked: root.sonos.refreshFavorites()
          }
        }

        Column {
          width: parent.width
          spacing: Style.space(5)
          visible: root.favoritesOpen

          Text {
            width: parent.width
            visible: root.favoriteStarting
            text: root.favoriteStartingTitle !== ""
              ? "Starting " + root.favoriteStartingTitle + "…" : ""
            color: Qt.darker(root.bar.foreground, 1.25)
            elide: Text.ElideRight
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            visible: !!root.sonos && root.sonos.favoriteError !== ""
            text: root.sonos ? root.sonos.favoriteError : ""
            color: root.bar.foreground
            wrapMode: Text.Wrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            visible: root.favorites.state === "error"
              || root.favorites.items.length === 0
              || root.favorites.unsupported > 0
            text: root.favorites.state === "error"
              ? "Favorites unavailable: " + root.favorites.error
              : root.favorites.items.length === 0
                ? "No playable Favorites found."
                : root.favorites.unsupported + " saved item"
                  + (root.favorites.unsupported === 1 ? "" : "s")
                  + " omitted because Sonos did not provide a playable source."
            color: Qt.darker(root.bar.foreground, 1.35)
            wrapMode: Text.Wrap
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Repeater {
            model: root.favorites.items
            Button {
              required property var modelData
              focusable: true
              width: parent.width
              text: modelData.title
              iconText: modelData.kind === "radio" ? "󰎆"
                : modelData.kind === "podcast" ? "󰦔" : "󰝚"
              foreground: root.bar.foreground
              bordered: true
              leftAlign: true
              enabled: !root.favoriteStarting
              tooltipText: "Play on " + root.roomLabel
              onClicked: {
                root.sonos.playFavorite(modelData.id, modelData.title)
              }
            }
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
            readonly property string playbackState: String(
              modelData.playbackState || "STOPPED"
            ).toUpperCase()
            readonly property bool audioPlaying: playbackState === "PLAYING"
            width: roomMixer.width
            spacing: Style.space(6)

            Row {
              id: roomIdentity
              width: Style.space(116)
              anchors.verticalCenter: parent.verticalCenter

              Text {
                width: roomIdentity.width
                anchors.verticalCenter: parent.verticalCenter
                text: roomRow.modelData.name
                color: roomRow.audioPlaying ? Color.accent : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: roomRow.audioPlaying
                elide: Text.ElideRight
              }
            }

            PanelSlider {
              id: roomVolume
              property real sentValue: Number.NaN
              bar: root.bar
              anchors.verticalCenter: parent.verticalCenter
              width: roomMixer.width - Style.space(166)
              minimum: 0
              maximum: 100
              step: 5
              value: Number(roomRow.modelData.volume || 0)
              onMoved: function(v) {
                if (isNaN(roomVolume.sentValue)) {
                  roomVolume.sentValue = v
                  root.sonos.setRoomVolume(roomRow.modelData.uid, v)
                }
              }
              onReleased: function(v) {
                if (isNaN(roomVolume.sentValue) || Math.abs(roomVolume.sentValue - v) >= 1)
                  root.sonos.setRoomVolume(roomRow.modelData.uid, v)
                roomVolume.sentValue = Number.NaN
              }
            }

            Button {
              iconText: roomRow.modelData.mute ? "󰝟" : "󰓄"
              focusable: true
              foreground: root.bar.foreground
              onClicked: root.sonos.setRoomMute(roomRow.modelData.uid, !roomRow.modelData.mute)
            }
          }
        }

        Button {
          text: root.groupSettingsOpen ? "Hide group settings" : "Group settings"
          focusable: true
          iconText: "󰒓"
          foreground: root.bar.foreground
          bordered: true
          onClicked: root.groupSettingsOpen = !root.groupSettingsOpen
        }

        Column {
          width: parent.width
          spacing: Style.space(5)
          visible: root.groupSettingsOpen

          Text {
            text: "Create or change the controlled group"
            color: Qt.darker(root.bar.foreground, 1.35)
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Flow {
            width: parent.width
            spacing: Style.space(5)

            Repeater {
              model: root.rooms
              Button {
                required property var modelData
                focusable: true
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
              focusable: true
              foreground: root.bar.foreground
              bordered: true
              enabled: !root.groupingApplying
              onClicked: root.stageEverywhere()
            }
            Button {
              text: "Cancel"
              focusable: true
              foreground: root.bar.foreground
              bordered: true
              enabled: root.groupingDirty && !root.groupingApplying
              onClicked: root.resetStagedRooms()
            }
            Button {
              text: root.groupingApplying ? "Applying…" : "Apply"
              focusable: true
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
    }
  }
}
