import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

FocusScope {
    id: card
    required property var ticket
    property string teamName: ""
    property bool dark: true
    property Item dragLayer
    signal selected(string ticketId)
    signal epicSelected(string epicId)
    width: parent ? parent.width : 260
    height: 147
    activeFocusOnTab: true
    Keys.onReturnPressed: selected(ticket.id)
    Accessible.role: Accessible.Button
    Accessible.name: ticket.key + ": " + ticket.title
    Accessible.onPressAction: selected(ticket.id)
    Rectangle {
        id: tile
        property string ticketId: card.ticket.id
        property int ticketRevision: card.ticket.revision
        width: card.width
        height: card.height - 12
        radius: 9
        color: card.dark ? (mouse.containsMouse ? "#243544" : "#1c2b38") : "#ffffff"
        border.color: card.activeFocus ? (card.dark ? "#8ad8bd" : "#247655") : (card.dark ? "#314756" : "#d6dfe7")
        Drag.active: mouse.drag.active
        Drag.keys: ["agilecode-ticket"]
        Drag.source: tile
        Drag.hotSpot.x: width / 2
        Drag.hotSpot.y: height / 2
        states: State {
            when: mouse.drag.active
            ParentChange { target: tile; parent: card.dragLayer }
            PropertyChanges { target: tile; z: 100 }
        }
        ColumnLayout {
            z: 1  // Keep the epic link above the card's drag/click area.
            anchors.fill: parent; anchors.margins: 13; spacing: 5
            RowLayout {
                Layout.fillWidth: true
                RowLayout {
                    spacing: 0
                    Text {
                        id: epicLink
                        text: card.ticket.epic_id || card.ticket.key
                        color: card.ticket.epic_id ? (card.dark ? "#8ad8bd" : "#266e58") : (card.dark ? "#9fb3c5" : "#587186")
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        font.underline: !!card.ticket.epic_id
                        elide: Text.ElideRight
                        Layout.maximumWidth: 112
                        MouseArea {
                            anchors.fill: parent
                            enabled: !!card.ticket.epic_id
                            cursorShape: Qt.PointingHandCursor
                            onClicked: card.epicSelected(card.ticket.epic_id)
                            ToolTip.visible: containsMouse && !!card.ticket.epic_id
                            ToolTip.text: "View epic " + (card.ticket.epic_id || "")
                            hoverEnabled: true
                        }
                    }
                    Text {
                        visible: !!card.ticket.epic_id
                        text: card.ticket.epic_id ? (card.ticket.key.indexOf(card.ticket.epic_id + "-") === 0 ? card.ticket.key.substring(card.ticket.epic_id.length) : " · " + card.ticket.key) : ""
                        color: card.dark ? "#9fb3c5" : "#587186"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                    }
                }
                Item { Layout.fillWidth: true }
                Text { text: card.ticket.priority > 0 ? "PRIORITY " + card.ticket.priority : ""; color: card.dark ? "#cba778" : "#8b5a20"; font.pixelSize: 10 }
            }
            Text {
                Layout.fillWidth: true; Layout.fillHeight: true
                text: card.ticket.title
                color: card.dark ? "#e7eef5" : "#243545"
                font.pixelSize: 14; font.weight: Font.DemiBold
                wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
            }
            Text {
                Layout.fillWidth: true
                text: card.teamName
                color: card.dark ? "#9fb3c5" : "#587186"
                font.pixelSize: 11
                elide: Text.ElideRight
            }
            RowLayout {
                Layout.fillWidth: true
                Rectangle { width: 6; height: 6; radius: 3; color: card.ticket.run_error ? (card.dark ? "#e6ab83" : "#985728") : (card.dark ? "#8ad8bd" : "#247655") }
                Text {
                    Layout.fillWidth: true
                    text: card.ticket.execution_state ? card.ticket.execution_state.replace(/_/g, " ") : "Not started"
                    color: card.dark ? "#a1b5c7" : "#5f7485"; font.pixelSize: 11; elide: Text.ElideRight
                }

            }
            Text {
                id: stepProgress
                Layout.fillWidth: true
                visible: !!card.ticket.step_total
                text: card.ticket.step_complete
                      ? "Steps complete (" + card.ticket.step_total + "/" + card.ticket.step_total + ")"
                      : (card.ticket.step_position
                         ? "Step " + card.ticket.step_position + "/" + card.ticket.step_total
                           + " · " + card.ticket.step_name : "")
                color: card.dark ? "#8da4b7" : "#637a8d"
                font.pixelSize: 10
                elide: Text.ElideRight
                ToolTip.visible: mouse.containsMouse && stepProgress.visible
                ToolTip.text: stepProgress.text
            }
        }
        MouseArea {
            id: mouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: drag.active ? Qt.ClosedHandCursor : Qt.PointingHandCursor
            drag.target: tile
            property bool moved: false
            onPressed: { moved = false; card.forceActiveFocus(); }
            onPositionChanged: if (drag.active) moved = true
            onReleased: { tile.Drag.drop(); tile.x = 0; tile.y = 0; }
            onClicked: if (!moved) card.selected(card.ticket.id)
        }
    }
}
