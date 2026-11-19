pragma Singleton
pragma ComponentBehavior: Bound
import QtQuick
import Quickshell.Io

QtObject {
    id: state
    property var devices: []
    property var cache: ({})
    property var preferences: ({})
    property int preferenceRevision: 0
    property string language: "en"
    readonly property string helper: decodeURIComponent(Qt.resolvedUrl("../helper.py").toString().replace(/^file:\/\//, ""))
    readonly property var defaults: ({mode: "view", minutes: 15, fields: null})
    function binding(d) { return [d.host, d.port, d.login, d.auth, d.keyPath, d.saveKeyPassphrase] }
    function matches(d, b) { return d && JSON.stringify(binding(d)) === JSON.stringify(b) }
    function info(d) {
        const s = d && cache[d.id]
        return s && matches(d, s.binding) ? s : null
    }
    function pref(d) { return (d && preferences[d.id]) || defaults }
    function configure(id, value) {
        preferences = Object.assign({}, preferences, {[id]: value})
        preferenceRevision++
        Qt.callLater(poll)
    }
    function sync(hosts, saved, prefs, revision) {
        const retained = {}, settings = {}
        for (const d of hosts) {
            const local = info(d), disk = saved && saved[d.id]
            if (disk && matches(d, disk.binding) && (!local || Date.parse(disk.fetchedAt) > (Date.parse(local.fetchedAt) || 0)))
                retained[d.id] = disk
            else if (local) retained[d.id] = local
            const p = prefs && revision === preferenceRevision ? prefs[d.id] : preferences[d.id]
            if (p) settings[d.id] = p
        }
        cache = retained
        preferences = settings
        devices = hosts
        Qt.callLater(poll)
    }
    function collecting(id) { return id !== "" && (first.hostId === id || second.hostId === id) }
    function lastAttempt(d) {
        const s = info(d)
        return s ? Math.max(Date.parse(s.fetchedAt) || 0, s.attempted || 0) : 0
    }
    function due(d, minutes) {
        const last = lastAttempt(d)
        return !last || Date.now() < last || Date.now() - last >= minutes * 60000
    }
    function collect(d, reason) {
        if (!d || !devices.some(host => host.id === d.id && matches(host, binding(d)))) return true
        const p = pref(d)
        if (reason !== "manual" && (reason !== p.mode || !due(d, reason === "view" ? 5 : p.minutes))) return true
        if (collecting(d.id)) return true
        const worker = !first.hostId ? first : !second.hostId ? second : null
        if (!worker) return false
        worker.hostId = d.id
        worker.identity = binding(d)
        worker.running = true
        return true
    }
    property var pendingManual: []
    function manual(d) {
        if (!collect(d, "manual") && !pendingManual.some(host => host.id === d.id))
            pendingManual = pendingManual.concat([d])
    }
    function resetAttempt(id) {
        const s = id && cache[id]
        if (!s) return
        cache = Object.assign({}, cache, {[id]: Object.assign({}, s, {attempted: 0})})
    }
    function poll() {
        while (pendingManual.length) {
            if (!collect(pendingManual[0], "manual")) return
            pendingManual = pendingManual.slice(1)
        }
        const scheduled = devices.filter(d => pref(d).mode === "schedule" && due(d, pref(d).minutes))
        scheduled.sort((a, b) => lastAttempt(a) - lastAttempt(b))
        for (const d of scheduled)
            if (!collect(d, "schedule")) break
    }
    function finish(worker, reply) {
        const id = worker.hostId
        const d = devices.find(host => host.id === id)
        if (matches(d, worker.identity)) {
            let next = Object.assign({}, info(d), {binding: worker.identity, fastfetchInstallToken: "", error: ""})
            try {
                const result = JSON.parse(reply)
                if (!result.ok) throw new Error(result.error || I18n.t(language, "Helper error"))
                const s = result.systemInfo
                if (!s || s.id !== id || !matches(d, s.binding)) throw new Error(I18n.t(language, "Invalid helper response"))
                next = Object.assign(next, s)
            } catch (error) {
                next.error = reply ? String(error.message) : I18n.t(language, "Helper did not respond; check Python and permissions")
            }
            next.attempted = Date.now()
            cache = Object.assign({}, cache, {[id]: next})
        }
        worker.hostId = ""
        Qt.callLater(poll)
    }
    component Worker: Process {
        id: worker
        property string hostId: ""
        property var identity: []
        command: ["python3", state.helper]
        stdinEnabled: true
        onStarted: write(JSON.stringify({op: "systemInfo", id: hostId, binding: identity, language: state.language}) + "\n")
        stdout: StdioCollector { id: output; waitForEnd: true }
        stderr: StdioCollector { waitForEnd: true }
        onExited: state.finish(worker, output.text || "")
    }
    property Worker first: Worker {}
    property Worker second: Worker {}
    property Timer scheduler: Timer {
        interval: 15000
        repeat: true
        running: state.devices.some(d => state.pref(d).mode === "schedule")
        onTriggered: state.poll()
    }
}
