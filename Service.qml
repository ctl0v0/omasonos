import QtQuick
import Quickshell
import Quickshell.Io

Item {
  id: root

  property var shell: null
  property var manifest: null

  readonly property string moduleName: "io.github.ctl0v0.omasonos"
  property var snapshot: ({
    type: "snapshot",
    version: 1,
    status: { state: "starting", message: "Starting OmaSonos…" },
    selectedAnchorRoomUid: "",
    targetGroupUid: "",
    households: [],
    target: null,
    favorites: { state: "not_loaded", items: [], total: 0, unsupported: 0, error: "" },
    playback: {
      state: "STOPPED",
      title: "",
      artist: "",
      album: "",
      artworkUrl: "",
      source: "UNKNOWN",
      positionSec: null,
      durationSec: null,
      availableActions: [],
      metadataState: "empty",
      stale: false
    }
  })
  property string commandError: ""
  property string processError: ""
  readonly property string lastError: commandError || processError
  property int requestCounter: 0
  property int restartAttempt: 0
  property bool expectedStop: false
  property bool backendReady: false
  property bool receivedSnapshotThisRun: false
  property bool setupFailed: false
  property string backendStderr: ""
  property int openPanelCount: 0
  property string favoriteRequestId: ""
  property string favoriteStartingTitle: ""
  property bool favoriteAwaitingSnapshot: false
  property string favoriteError: ""
  property string moveRequestId: ""
  property string moveError: ""

  readonly property bool ready: backendReady && snapshot && snapshot.status && snapshot.status.state === "ready"
  readonly property var playback: snapshot && snapshot.playback ? snapshot.playback : ({})
  readonly property var target: snapshot ? snapshot.target : null
  readonly property var households: snapshot && snapshot.households ? snapshot.households : []

  function setStatus(state, message) {
    var next = {}
    for (var key in snapshot) next[key] = snapshot[key]
    var status = {}
    var currentStatus = snapshot && snapshot.status ? snapshot.status : ({})
    for (var statusKey in currentStatus) status[statusKey] = currentStatus[statusKey]
    status.state = state
    status.message = message
    next.status = status
    snapshot = next
  }

  function localPath(url) {
    var text = String(url || "")
    if (text.indexOf("file://") === 0) text = text.substring(7)
    return decodeURIComponent(text)
  }

  readonly property string backendPath: localPath(Qt.resolvedUrl("sonos-backend"))

  function sendCommand(op, args) {
    if (!backend.running) {
      commandError = "OmaSonos backend is not running"
      return ""
    }
    requestCounter += 1
    var id = String(requestCounter)
    if (op !== "setPanelOpen") commandError = ""
    var payload = { id: id, op: op }
    var fields = args || ({})
    for (var key in fields) payload[key] = fields[key]
    backend.write(JSON.stringify(payload) + "\n")
    return id
  }

  function refresh() {
    if (setupFailed) {
      setupFailed = false
      processError = ""
      backendStderr = ""
      restartAttempt = 0
      setStatus("starting", "Starting OmaSonos…")
      if (!backend.running) backend.running = true
      return
    }
    sendCommand("refresh")
  }
  function setPanelOpen(open) {
    var wasOpen = openPanelCount > 0
    openPanelCount = Math.max(0, openPanelCount + (open ? 1 : -1))
    var isOpen = openPanelCount > 0
    if (wasOpen !== isOpen && backend.running)
      sendCommand("setPanelOpen", { open: isOpen })
  }
  function playPause() { sendCommand("playPause") }
  function next() { sendCommand("next") }
  function previous() { sendCommand("previous") }
  function seek(positionSec) { sendCommand("seek", { positionSec: positionSec }) }
  function playFavorite(favoriteId, title) {
    if (favoriteRequestId !== "" || favoriteAwaitingSnapshot) return ""
    favoriteStartingTitle = String(title || "Favorite")
    favoriteError = ""
    favoriteRequestId = sendCommand("playFavorite", { favoriteId: favoriteId })
    return favoriteRequestId
  }
  function refreshFavorites() { sendCommand("refreshFavorites") }
  function movePlaybackToRoom(roomUid) {
    if (moveRequestId !== "") return ""
    moveError = ""
    moveRequestId = sendCommand("movePlaybackToRoom", { roomUid: roomUid })
    return moveRequestId
  }
  function selectGroup(groupUid) { sendCommand("selectGroup", { groupUid: groupUid }) }
  function setGroupVolume(volume) { sendCommand("setGroupVolume", { volume: volume }) }
  function adjustGroupVolume(delta) { sendCommand("adjustGroupVolume", { delta: delta }) }
  function setGroupMute(mute) { sendCommand("setGroupMute", { mute: !!mute }) }
  function setRoomVolume(roomUid, volume) { sendCommand("setRoomVolume", { roomUid: roomUid, volume: volume }) }
  function setRoomMute(roomUid, mute) { sendCommand("setRoomMute", { roomUid: roomUid, mute: !!mute }) }
  function applyMembers(roomUids) { sendCommand("applyMembers", { roomUids: roomUids }) }

  function handleLine(line) {
    var text = String(line || "").trim()
    if (!text) return
    var message
    try {
      message = JSON.parse(text)
    } catch (e) {
      commandError = "Backend emitted invalid JSON"
      console.warn("OmaSonos invalid stdout:", text)
      return
    }
    if (message.type === "snapshot") {
      var firstSnapshot = !receivedSnapshotThisRun
      snapshot = message
      backendReady = true
      receivedSnapshotThisRun = true
      setupFailed = false
      restartAttempt = 0
      processError = ""
      if (favoriteAwaitingSnapshot) {
        favoriteAwaitingSnapshot = false
        favoriteStartingTitle = ""
      }
      if (firstSnapshot && openPanelCount > 0)
        sendCommand("setPanelOpen", { open: true })
      return
    }
    if (message.type === "result" && message.ok === false) {
      commandError = String(message.error || "Sonos command failed")
      if (String(message.id || "") === moveRequestId) {
        moveError = commandError
        moveRequestId = ""
      }
      if (String(message.id || "") === favoriteRequestId) {
        favoriteError = "Could not start " + favoriteStartingTitle + ": "
          + String(message.error || "Sonos command failed")
        favoriteRequestId = ""
        favoriteAwaitingSnapshot = false
        favoriteStartingTitle = ""
      }
    } else if (message.type === "result" && message.ok === true) {
      if (String(message.id || "") === moveRequestId) {
        moveError = ""
        moveRequestId = ""
      }
      if (String(message.id || "") === favoriteRequestId) {
        favoriteRequestId = ""
        favoriteAwaitingSnapshot = true
      }
    }
  }

  Process {
    id: backend
    command: [root.backendPath]
    stdinEnabled: true

    stdout: SplitParser {
      onRead: function(line) { root.handleLine(line) }
    }

    stderr: SplitParser {
      onRead: function(line) {
        var text = String(line || "").trim()
        if (!text) return
        var marker = "OMASONOS_SETUP_ERROR:"
        if (text.indexOf(marker) === 0)
          root.backendStderr = text.substring(marker.length).trim()
        console.warn("OmaSonos:", text)
      }
    }

    onStarted: {
      root.backendReady = false
      root.receivedSnapshotThisRun = false
      root.backendStderr = ""
    }

    onExited: function(exitCode) {
      if (root.expectedStop) return
      root.backendReady = false
      root.processError = root.backendStderr !== ""
        ? root.backendStderr
        : "OmaSonos backend stopped (" + exitCode + ")"
      if (root.favoriteRequestId !== "" || root.favoriteAwaitingSnapshot) {
        root.favoriteError = "Could not start " + root.favoriteStartingTitle
          + ": the OmaSonos backend stopped"
      }
      root.favoriteRequestId = ""
      root.favoriteAwaitingSnapshot = false
      root.favoriteStartingTitle = ""
      if (root.moveRequestId !== "") {
        root.moveError = "Could not move playback: the OmaSonos backend stopped"
      }
      root.moveRequestId = ""
      if (!root.receivedSnapshotThisRun && root.backendStderr !== "") {
        root.setupFailed = true
        root.setStatus("setup_error", root.processError)
        return
      }
      root.setStatus("starting", "OmaSonos stopped and will restart automatically…")
      root.restartAttempt = Math.min(root.restartAttempt + 1, 6)
      restartTimer.interval = Math.min(30000, 1000 * Math.pow(2, root.restartAttempt - 1))
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    repeat: false
    onTriggered: if (!root.expectedStop && !backend.running) backend.running = true
  }

  Component.onCompleted: backend.running = true
  Component.onDestruction: {
    expectedStop = true
    backend.running = false
  }
}
