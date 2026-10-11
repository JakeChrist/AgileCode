pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ListView {
    id: conversation
    objectName: "conversationView"
    property string sessionId: ""
    property var messages: []
    property bool dark: true
    property string assistantName: "Assistant"
    property bool interrupted: false
    property bool reasoningExpandedByDefault: false
    property string liveText: ""
    property string liveReasoning: ""
    property var liveToolCalls: []
    property string liveRequestId: ""
    property var pendingRows: []
    property var savedRequests: ({})
    property bool followTail: true
    property bool liveReasoningExpanded: false
    property var readingAnchor: null
    property bool restoringPosition: false
    property bool anchorNeedsRestore: false
    signal olderHistoryRequested()
    readonly property bool hasLiveMessage: liveText.length > 0 || liveReasoning.length > 0 || liveToolCalls.length > 0
    clip: true
    spacing: 8
    boundsBehavior: Flickable.StopAtBounds
    model: messageModel
    ScrollBar.vertical: ScrollBar {
        id: conversationScroll
        objectName: "conversationScrollBar"
        onPressedChanged: {
            if (pressed) conversation.beginInteraction();
            else conversation.finishInteraction();
        }
    }
    // Observe the gesture, but let the native ListView receive the wheel event.
    WheelHandler {
        target: null
        blocking: false
        onWheel: function(event) {
            conversation.beginInteraction();
            interactionTimer.restart();
        }
    }
    Timer {
        id: interactionTimer
        interval: 120
        onTriggered: conversation.finishInteraction()
    }
    ListModel { id: messageModel; dynamicRoles: true }
    // Frame-rate batching: the provider can emit dozens of tokens per second.
    // Update the Qt text document at most once per 100 ms, not once per token.
    Timer {
        id: liveFlushTimer
        interval: 100
        repeat: false
        onTriggered: conversation.syncLiveRow()
    }

    function liveRowIndex() {
        for (var i = 0; i < messageModel.count; ++i)
            if (messageModel.get(i).recordId === "__streaming__") return i;
        return -1;
    }

    function liveBody() {
        return {
            role: "assistant",
            content: liveText,
            reasoning_content: liveReasoning,
            tool_calls: liveToolCalls.filter(function(call) { return !!call; })
        };
    }

    function syncLiveRow() {
        var index = liveRowIndex();
        if (!hasLiveMessage) {
            if (index >= 0) messageModel.remove(index);
            return;
        }
        var row = {
            recordId: "__streaming__",
            requestId: liveRequestId,
            messageBody: liveBody(),
            results: ({}),
            interrupted: interrupted,
            isStreaming: true,
            renderVersion: "live-" + liveRequestId
        };
        if (index < 0) {
            messageModel.append(row);
        } else {
            messageModel.setProperty(index, "messageBody", row.messageBody);
            messageModel.setProperty(index, "interrupted", row.interrupted);
            messageModel.setProperty(index, "isStreaming", true);
        }
        scheduleTailScroll();
    }

    function resetLive(preserveIncomplete) {
        liveFlushTimer.stop();
        // Tool-use rounds can start before the saved history has returned.
        // Keep the preceding completed round visible until it is persisted.
        if (preserveIncomplete && liveRequestId && hasLiveMessage && !savedRequests[liveRequestId]) {
            var retained = pendingRows.filter(function(row) { return row.requestId !== liveRequestId; });
            retained.push({
                recordId: "__pending__" + liveRequestId,
                requestId: liveRequestId,
                messageBody: liveBody(),
                results: ({}),
                interrupted: false,
                isStreaming: false,
                renderVersion: "pending-" + liveRequestId
            });
            pendingRows = retained;
        }
        var index = liveRowIndex();
        if (index >= 0) {
            if (preserveIncomplete && pendingRows.length &&
                pendingRows[pendingRows.length - 1].requestId === liveRequestId) {
                var parked = pendingRows[pendingRows.length - 1];
                ["recordId", "messageBody", "interrupted", "isStreaming", "renderVersion"].forEach(function(key) {
                    messageModel.setProperty(index, key, parked[key]);
                });
            } else {
                messageModel.remove(index);
            }
        }
        liveText = "";
        liveReasoning = "";
        liveToolCalls = [];
        liveRequestId = "";
        liveReasoningExpanded = reasoningExpandedByDefault;
    }

    function appendDelta(event) {
        if (!sessionId || event.session_id !== sessionId) return;
        var requestId = event.data.request_id;
        // Ignore late packets for a turn already present in saved history.
        if (savedRequests[requestId]) return;
        if (liveRequestId !== requestId) {
            resetLive(true);
            liveRequestId = requestId;
        }
        if (event.kind === "ModelTextDelta") {
            liveText += event.data.value;
        } else if (event.kind === "ModelReasoningDelta") liveReasoning += event.data.value;
        else if (event.kind === "ModelToolDelta") {
            var fragment = event.data.value;
            var calls = liveToolCalls.slice();
            var callIndex = fragment.index;
            var call = calls[callIndex] || {id: "", function: {name: "", arguments: ""}};
            if (fragment.id) call.id = fragment.id;
            var fn = fragment.function || {};
            if (fn.name && call.function.name !== fn.name) call.function.name += fn.name;
            if (fn.arguments) call.function.arguments += typeof fn.arguments === "string" ? fn.arguments : JSON.stringify(fn.arguments);
            calls[callIndex] = call;
            liveToolCalls = calls;
        }
        if (liveRowIndex() < 0) syncLiveRow();
        else if (!liveFlushTimer.running) liveFlushTimer.start();
    }

    function updateMessages() {
        captureAnchor();
        var rows = [];
        var consumed = ({});
        var saved = ({});
        for (var i = 0; i < messages.length; ++i) {
            var record = messages[i];
            if (consumed[i]) continue;
            var results = ({});
            if (record.body.role === "assistant") {
                if (record.request_id) saved[record.request_id] = true;
                var calls = record.body.tool_calls || [];
                for (var j = i + 1; j < messages.length && messages[j].body.role === "tool"; ++j) {
                    var result = messages[j].body;
                    if (calls.some(function(call) { return call.id === result.tool_call_id; })) {
                        results[result.tool_call_id] = result;
                        consumed[j] = true;
                    }
                }
            }
            rows.push({
                recordId: record.id || String(record.seq),
                requestId: record.request_id || "",
                messageBody: record.body,
                results: results,
                interrupted: !!record.partial,
                isStreaming: false,
                renderVersion: (record.id || String(record.seq)) + "|" + (!!record.partial) + "|" +
                    Object.keys(results).map(function(id) { return id + ":" + (results[id].id || ""); }).join(",")
            });
        }

        pendingRows = pendingRows.filter(function(row) { return !saved[row.requestId]; });
        for (var p = 0; p < pendingRows.length; ++p) rows.push(pendingRows[p]);
        var savedLive = !!(liveRequestId && saved[liveRequestId]);
        if (hasLiveMessage && !savedLive) {
            rows.push({recordId: "__streaming__", requestId: liveRequestId,
                       messageBody: liveBody(), results: ({}), interrupted: interrupted,
                       isStreaming: true, renderVersion: "live-" + liveRequestId});
        }
        // Insert older history without replacing existing delegates. Promote the
        // live row in place when its persisted assistant message arrives.
        for (var n = 0; n < rows.length; ++n) {
            var found = -1;
            for (var k = n; k < messageModel.count; ++k) {
                var prior = messageModel.get(k);
                if (prior.recordId === rows[n].recordId ||
                    ((prior.isStreaming || prior.recordId.indexOf("__pending__") === 0) &&
                     rows[n].messageBody.role === "assistant" &&
                     prior.requestId === rows[n].requestId)) { found = k; break; }
            }
            if (found < 0) messageModel.insert(n, rows[n]);
            else {
                if (found !== n) messageModel.move(found, n, 1);
                if (readingAnchor && readingAnchor.id === messageModel.get(n).recordId)
                    readingAnchor.id = rows[n].recordId;
                // Saved records never change in place. Their IDs and result
                // message IDs suffice to detect updates; never stringify an
                // entire reasoning transcript or tool result for comparison.
                if (messageModel.get(n).renderVersion !== rows[n].renderVersion ||
                    messageModel.get(n).recordId !== rows[n].recordId ||
                    messageModel.get(n).isStreaming !== rows[n].isStreaming) {
                    ["recordId", "requestId", "messageBody", "results", "interrupted", "isStreaming", "renderVersion"].forEach(function(key) {
                        messageModel.setProperty(n, key, rows[n][key]);
                    });
                }
            }
        }
        if (messageModel.count > rows.length)
            messageModel.remove(rows.length, messageModel.count - rows.length);

        savedRequests = saved;
        if (savedLive) {
            liveFlushTimer.stop();
            liveText = "";
            liveReasoning = "";
            liveToolCalls = [];
            liveRequestId = "";
        }
        syncLiveRow();
        if (!followTail && readingAnchor) {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
        scheduleTailScroll();
    }

    function tailY() {
        return originY + Math.max(0, contentHeight - height);
    }

    function isAtTail(tolerance) {
        return Math.abs(contentY - tailY()) <= (tolerance === undefined ? 2 : tolerance);
    }

    function stopFollowing() {
        followTail = false;
        tailScrollTimer.stop();
    }

    function beginInteraction() {
        stopFollowing();
        captureAnchor();
    }

    function finishInteraction() {
        // Resume following only after the user has actually returned to the
        // bottom. Their former position is not evidence they want to follow.
        if (isAtTail(2)) {
            followTail = true;
            readingAnchor = null;
            anchorNeedsRestore = false;
            scheduleTailScroll();
        } else {
            stopFollowing();
            captureAnchor();
        }
        requestOlderHistoryIfNeeded();
    }

    function requestOlderHistoryIfNeeded() {
        if (!followTail && height > 0 && contentHeight > height
                && contentY <= originY + 48)
            olderHistoryRequested();
    }

    function captureAnchor() {
        if (followTail || restoringPosition) return;
        var index = indexAt(1, contentY + spacing + 1);
        var item = itemAtIndex(index);
        if (item) readingAnchor = {id: messageModel.get(index).recordId, offset: item.y - contentY};
    }

    function scheduleAnchorRestore() {
        if (!followTail && readingAnchor && anchorNeedsRestore && !anchorRestoreTimer.running)
            anchorRestoreTimer.start();
    }

    function restoreAnchor() {
        if (followTail || moving || conversationScroll.pressed || !readingAnchor) return;
        restoringPosition = true;
        forceLayout();
        for (var i = 0; i < messageModel.count; ++i) {
            if (messageModel.get(i).recordId !== readingAnchor.id) continue;
            var item = itemAtIndex(i);
            if (!item) {
                positionViewAtIndex(i, ListView.Beginning);
                forceLayout();
                item = itemAtIndex(i);
            }
            if (item) contentY = item.y - readingAnchor.offset;
            break;
        }
        anchorNeedsRestore = false;
        restoringPosition = false;
    }

    function scheduleTailScroll() {
        if (followTail && !moving && !conversationScroll.pressed && !tailScrollTimer.running)
            tailScrollTimer.start();
    }

    function scrollToTail() {
        if (!followTail || moving || conversationScroll.pressed) return;
        var targetY = tailY();
        if (Math.abs(contentY - targetY) > 0.5)
            contentY = targetY;
    }

    function jumpToTail() {
        cancelFlick();
        interactionTimer.stop();
        readingAnchor = null;
        anchorNeedsRestore = false;
        followTail = true;
        tailScrollTimer.stop();
        scrollToTail();
        scheduleTailScroll();
    }

    Timer {
        id: tailScrollTimer
        interval: 0
        repeat: false
        onTriggered: conversation.scrollToTail()
    }
    Timer {
        id: anchorRestoreTimer
        interval: 0
        repeat: false
        onTriggered: conversation.restoreAnchor()
    }

    onMessagesChanged: updateMessages()
    onInterruptedChanged: syncLiveRow()
    onSessionIdChanged: {
        resetLive(false);
        messageModel.clear();
        pendingRows = [];
        savedRequests = ({});
        followTail = true;
        readingAnchor = null;
        anchorNeedsRestore = false;
        tailScrollTimer.stop();
        anchorRestoreTimer.stop();
    }
    onContentHeightChanged: {
        if (followTail) scheduleTailScroll();
        else scheduleAnchorRestore();
    }
    onHeightChanged: {
        if (followTail) scheduleTailScroll();
        else {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
    }
    onOriginYChanged: {
        if (followTail) scheduleTailScroll();
        else {
            anchorNeedsRestore = true;
            scheduleAnchorRestore();
        }
    }
    onMovementStarted: beginInteraction()
    onContentYChanged: {
        if (restoringPosition) return;
        if (moving || conversationScroll.pressed || interactionTimer.running) {
            if (followTail) stopFollowing();
            captureAnchor();
        }
        requestOlderHistoryIfNeeded();
    }
    onMovementEnded: finishInteraction()

    delegate: ChatMessage {
        required property string recordId
        required property var messageBody
        required property var results
        required property bool interrupted
        required property bool isStreaming
        objectName: recordId === "__streaming__" ? "streamingMessage" : "chatMessage-" + recordId
        width: conversation.width
        body: messageBody
        toolResults: results
        partial: interrupted
        streaming: isStreaming
        dark: conversation.dark
        reasoningExpandedByDefault: conversation.reasoningExpandedByDefault
        assistantName: conversation.assistantName
        onDisclosureToggling: {
            conversation.beginInteraction();
            conversation.captureAnchor();
            conversation.anchorNeedsRestore = true;
            conversation.scheduleAnchorRestore();
        }
        onReasoningToggled: function(expanded) {
            if (recordId !== "__streaming__") return;
            conversation.liveReasoningExpanded = expanded;

        }
    }
}
