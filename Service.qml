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
    playback: {
      state: "STOPPED",
      title: "",
      artist: "",
      album: "",
      artworkUrl: "",
      source: "UNKNOWN",
      positionSec: null,
      durationSec: null,
      availableActions: []
    }
  })
  property string lastError: ""
  property int requestCounter: 0
  property int restartAttempt: 0
  property bool expectedStop: false

  readonly property bool ready: snapshot && snapshot.status && snapshot.status.state === "ready"
  readonly property var playback: snapshot && snapshot.playback ? snapshot.playback : ({})
  readonly property var target: snapshot ? snapshot.target : null
  readonly property var households: snapshot && snapshot.households ? snapshot.households : []

  function localPath(url) {
    var text = String(url || "")
    if (text.indexOf("file://") === 0) text = text.substring(7)
    return decodeURIComponent(text)
  }

  readonly property string backendPath: localPath(Qt.resolvedUrl("sonos-backend"))

  function sendCommand(op, args) {
    if (!backend.running) {
      lastError = "OmaSonos backend is not running"
      return ""
    }
    requestCounter += 1
    var id = String(requestCounter)
    var payload = { id: id, op: op }
    var fields = args || ({})
    for (var key in fields) payload[key] = fields[key]
    backend.write(JSON.stringify(payload) + "\n")
    return id
  }

  function refresh() { sendCommand("refresh") }
  function setPanelOpen(open) { sendCommand("setPanelOpen", { open: !!open }) }
  function playPause() { sendCommand("playPause") }
  function next() { sendCommand("next") }
  function previous() { sendCommand("previous") }
  function seek(positionSec) { sendCommand("seek", { positionSec: positionSec }) }
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
      lastError = "Backend emitted invalid JSON"
      console.warn("OmaSonos invalid stdout:", text)
      return
    }
    if (message.type === "snapshot") {
      snapshot = message
      return
    }
    if (message.type === "result" && message.ok === false) {
      lastError = String(message.error || "Sonos command failed")
    } else if (message.type === "result" && message.ok === true) {
      lastError = ""
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
        if (text) console.warn("OmaSonos:", text)
      }
    }

    onStarted: {
      root.restartAttempt = 0
      root.lastError = ""
    }

    onExited: function(exitCode) {
      if (root.expectedStop) return
      root.lastError = "OmaSonos backend stopped (" + exitCode + ")"
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
