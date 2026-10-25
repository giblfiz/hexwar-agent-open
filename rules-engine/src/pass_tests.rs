//! hexwar-lab-v13: v12-fan plus the pass rule. A side with no legal action
//! passes; the clock advances; king capture and the deadline still decide.
use super::*;

fn p(q: i32, r: i32, kind: Kind, side: u8, facing: u8) -> Piece {
    Piece { q, r, kind, side, facing }
}
fn st(version: &str, board: Vec<Piece>, turn: u8, ply: u64) -> State {
    let mut s = State {
        rules_version: version.into(), radius: 4, board,
        turn, reserves: [0, 0], ply, history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
fn pass() -> Action {
    action("pass", None, None, None)
}
// White (to move) has only its King at (0,3), rooted by a black Enchanter at
// (0,2) facing 3 (fan: (0,3), (1,2), (-1,3)). A black Guard at (1,2) stands
// adjacent to the frozen king.
fn frozen_king(version: &str) -> State {
    st(version, vec![
        p(0, 3, Kind::King, 0, 0),
        p(0, 2, Kind::Enchanter, 1, 3),
        p(1, 2, Kind::Guard, 1, 3),
        p(0, -4, Kind::King, 1, 3),
    ], 0, 0)
}
// Every piece on both sides is rooted: each king sits in an enemy
// Enchanter's fan, and each Enchanter pair roots one another. White also has a
// rooted Guard at (-1,3), so the deadline cascade resolves on piece count.
fn mutual_lock(version: &str, ply: u64) -> State {
    st(version, vec![
        p(0, 3, Kind::King, 0, 0),
        p(-1, 3, Kind::Guard, 0, 0),
        p(0, 2, Kind::Enchanter, 1, 3),
        p(1, 2, Kind::Enchanter, 0, 5),
        p(0, -3, Kind::King, 1, 3),
        p(0, -2, Kind::Enchanter, 0, 0),
        p(-1, -2, Kind::Enchanter, 1, 2),
    ], 0, ply)
}

#[test]
fn v13_side_without_actions_has_exactly_pass() {
    let s = frozen_king("hexwar-lab-v13");
    assert!(moves_for_activity(&s, true).is_empty());
    assert_eq!(moves(&s), vec![pass()]);
    assert_eq!(outcome(&s).status, "ongoing");
    let legal = process(json!({"op":"legal","state":serde_json::to_value(&s).unwrap()})).unwrap();
    assert_eq!(legal["actions"], json!([{"kind":"pass","from":null,"to":null,"facing":null}]));
    // JSON without explicit nulls parses to the same action.
    let applied = process(json!({"op":"apply","state":serde_json::to_value(&s).unwrap(),
        "action":{"kind":"pass"}})).unwrap();
    let n: State = serde_json::from_value(applied["state"].clone()).unwrap();
    assert_eq!((n.ply, n.turn), (1, 1));
    assert_eq!(n.board, s.board);
    assert_eq!(n.history, vec![key(&s)]);
    assert_eq!(applied["outcome"]["status"], "ongoing");
    // The opponent may now capture the frozen king.
    let capture = action("move", Some([1, 2]), Some([0, 3]), None);
    assert!(moves(&n).contains(&capture));
    let o = outcome(&apply_unchecked(&n, &capture));
    assert_eq!((o.status, o.winner, o.reason), ("win", Some(1), "king_lost"));
    // Search at the pass node returns the pass; the reply search finds the kill.
    let r = process(json!({"op":"search","state":serde_json::to_value(&s).unwrap(),
        "depth":3,"node_budget":5000,"quiescence":true,"policy":"tactical"})).unwrap();
    assert_eq!(r["action"]["kind"], "pass");
    assert_eq!(r["value"], json!(-(WIN - 2)));
    let r = process(json!({"op":"search","state":serde_json::to_value(&n).unwrap(),
        "depth":2,"node_budget":5000,"quiescence":true})).unwrap();
    assert_eq!(r["action"], serde_json::to_value(&capture).unwrap());
}
#[test]
fn v13_pass_illegal_when_any_other_action_exists() {
    let s = frozen_king("hexwar-lab-v13");
    let n = apply_unchecked(&s, &pass());
    assert!(!moves(&n).contains(&pass()));
    let r = process(json!({"op":"apply","state":serde_json::to_value(&n).unwrap(),
        "action":{"kind":"pass"}}));
    assert_eq!(r.unwrap_err(), "illegal action");
}
#[test]
fn v13_mutual_passes_run_the_clock_to_the_deadline_cascade() {
    let mut s = mutual_lock("hexwar-lab-v13", 0);
    let mut passes_seen = 0;
    while outcome(&s).status == "ongoing" {
        assert_eq!(moves(&s), vec![pass()]);
        s = apply_unchecked(&s, &pass());
        passes_seen += 1;
    }
    assert_eq!(passes_seen, 84);
    assert_eq!(s.ply, deadline(&s));
    let o = outcome(&s);
    assert_eq!((o.status, o.winner, o.reason), ("win", Some(0), "deadline_piece_count"));
    // Clocked keys differ each ply, so passing never repeats a position.
    let mut keys = s.history.clone();
    keys.sort();
    keys.dedup();
    assert_eq!(keys.len(), 84);
    // Search from a mutual-lock node near the deadline passes and sees the win.
    let near = mutual_lock("hexwar-lab-v13", 80);
    let r = process(json!({"op":"search","state":serde_json::to_value(&near).unwrap(),
        "depth":6,"node_budget":100000,"quiescence":true})).unwrap();
    assert_eq!(r["action"]["kind"], "pass");
    assert_eq!(r["completed_depth"], 6);
    assert_eq!(r["value"], json!(WIN - 4));
}
#[test]
fn v12_and_v12_fan_keep_stalemate_draw() {
    for version in ["hexwar-lab-v12", "hexwar-lab-v12-fan"] {
        for s in [frozen_king(version), mutual_lock(version, 0)] {
            assert!(moves(&s).is_empty(), "{version}");
            let o = outcome(&s);
            assert_eq!((o.status, o.reason), ("draw", "stalemate"), "{version}");
            let r = process(json!({"op":"apply","state":serde_json::to_value(&s).unwrap(),
                "action":{"kind":"pass"}}));
            assert!(r.is_err(), "{version} must reject pass");
        }
    }
}
#[test]
fn v13_accepted_wherever_v12_fan_is() {
    let s = frozen_king("hexwar-lab-v13");
    assert!(timed(&s));
    assert_eq!(deadline(&s), 84);
    // Fan rooting, not all-around rooting: the Guard beside the Enchanter
    // in its fan is black's own piece; check a white piece at the Enchanter's rear.
    let rear = st("hexwar-lab-v13", vec![
        p(0, 3, Kind::King, 0, 0), p(0, 1, Kind::Guard, 0, 0),
        p(0, 2, Kind::Enchanter, 1, 3), p(0, -4, Kind::King, 1, 3),
    ], 0, 0);
    assert!(!rooted(&rear, &rear.board[at(&rear, [0, 1]).unwrap()]));
    let catalog = process(json!({"op":"catalog","rules_version":"hexwar-lab-v13"})).unwrap();
    let fan = process(json!({"op":"catalog","rules_version":"hexwar-lab-v12-fan"})).unwrap();
    assert_eq!(catalog["pieces"], fan["pieces"]);
    let mut as_fan = s.clone();
    as_fan.rules_version = "hexwar-lab-v12-fan".into();
    let r13 = process(json!({"op":"evaluate","state":serde_json::to_value(&s).unwrap()})).unwrap();
    let rfan = process(json!({"op":"evaluate","state":serde_json::to_value(&as_fan).unwrap()})).unwrap();
    assert_eq!(r13["material_values"], rfan["material_values"]);
    assert!(r13["material_values"].get("enchanter").is_some());
}
