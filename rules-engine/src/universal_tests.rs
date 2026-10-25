//! hexwar-lab-v14: V13 plus the Fey Universal. It swaps with any friendly
//! piece anywhere; the arriving friend picks its facing; it never captures.
use super::*;

fn p(q: i32, r: i32, kind: Kind, side: u8, facing: u8) -> Piece {
    Piece { q, r, kind, side, facing }
}
fn raw(version: &str, board: Vec<Piece>, turn: u8) -> State {
    State {
        rules_version: version.into(), radius: 4, board,
        turn, reserves: [0, 0], ply: 0, history: vec![],
    }
}
fn st(version: &str, board: Vec<Piece>, turn: u8) -> State {
    let mut s = raw(version, board, turn);
    validate(&mut s).unwrap();
    s
}
fn piece<'a>(s: &'a State, q: i32, r: i32) -> &'a Piece {
    &s.board[at(s, [q, r]).unwrap()]
}
fn from(s: &State, q: i32, r: i32) -> Vec<Action> {
    moves(s).into_iter().filter(|a| a.from == Some([q, r])).collect()
}
// White Universal at (-3,4) behind its Crowned Lancer at (-2,4); a friendly
// Guard at (2,-3) is far away and the line to it is blocked by the Lancer.
// A black Guard is adjacent to the Universal (capturable target, never taken).
fn board() -> Vec<Piece> {
    vec![
        p(-3, 4, Kind::Universal, 0, 0),
        p(-2, 4, Kind::CrownedLancer, 0, 0),
        p(2, -3, Kind::Guard, 0, 0),
        p(-3, 3, Kind::Guard, 1, 3),
        p(0, -4, Kind::King, 1, 3),
    ]
}

#[test]
fn v14_universal_swaps_with_far_blocked_friend_and_royal_only() {
    let s = st("hexwar-lab-v14", board(), 0);
    let acts = from(&s, -3, 4);
    // Two friends x six facings; no move, rotate or capture.
    assert_eq!(acts.len(), 12);
    assert!(acts.iter().all(|a| a.kind == "swap"));
    for target in [[2, -3], [-2, 4]] {
        for f in 0..6u8 {
            assert!(acts.contains(&action("swap", Some([-3, 4]), Some(target), Some(f))));
        }
    }
    assert!(!acts.iter().any(|a| a.to == Some([-3, 3])), "never captures");
    assert!(!can_capture(&s, piece(&s, -3, 4), [-3, 3]));
}

#[test]
fn v14_far_swap_arriving_friend_chooses_facing_universal_keeps_its_own() {
    let mut b = board();
    b[0].facing = 2;
    let s = st("hexwar-lab-v14", b, 0);
    let a = action("swap", Some([-3, 4]), Some([2, -3]), Some(4));
    let r = process(json!({"op":"apply","state":serde_json::to_value(&s).unwrap(),
        "action":serde_json::to_value(&a).unwrap()})).unwrap();
    let n: State = serde_json::from_value(r["state"].clone()).unwrap();
    let u = piece(&n, 2, -3);
    assert_eq!((u.kind, u.facing), (Kind::Universal, 2));
    let g = piece(&n, -3, 4);
    assert_eq!((g.kind, g.facing), (Kind::Guard, 4));
    assert_eq!(n.board.len(), s.board.len());
}

#[test]
fn v14_universal_swaps_with_royal() {
    let s = st("hexwar-lab-v14", board(), 0);
    let n = apply_unchecked(&s, &action("swap", Some([-3, 4]), Some([-2, 4]), Some(3)));
    let k = piece(&n, -3, 4);
    assert_eq!((k.kind, k.facing), (Kind::CrownedLancer, 3));
    assert_eq!(piece(&n, -2, 4).kind, Kind::Universal);
    assert_eq!(outcome(&n).status, "ongoing");
}

#[test]
fn v14_rooted_universal_cannot_act_under_fan_rules() {
    // Black Enchanter at (-3,3) facing 3: fan = DIRS 3,4,2 -> (-3,4) rooted.
    let mut b = board();
    b[3] = p(-3, 3, Kind::Enchanter, 1, 3);
    let s = st("hexwar-lab-v14", b.clone(), 0);
    assert!(rooted(&s, piece(&s, -3, 4)));
    assert!(from(&s, -3, 4).is_empty());
    // Facing away (0): the Universal is only rear-adjacent, so it acts.
    b[3].facing = 0;
    let s = st("hexwar-lab-v14", b, 0);
    assert!(!rooted(&s, piece(&s, -3, 4)));
    assert_eq!(from(&s, -3, 4).len(), 12);
}

#[test]
fn v14_universal_is_capturable() {
    let s = st("hexwar-lab-v14", board(), 1);
    let a = action("move", Some([-3, 3]), Some([-3, 4]), None);
    assert!(moves(&s).contains(&a));
    let n = apply_unchecked(&s, &a);
    assert!(!n.board.iter().any(|x| x.kind == Kind::Universal));
}

#[test]
fn universal_rejected_before_v14_and_more_than_one_rejected() {
    for v in ["hexwar-lab-v13", "hexwar-lab-v12-fan", "hexwar-lab-v12"] {
        let mut s = raw(v, board(), 0);
        assert_eq!(validate(&mut s).unwrap_err(), "Universal requires hexwar-lab-v14");
    }
    let mut b = board();
    b.push(p(3, -3, Kind::Universal, 0, 0));
    let mut s = raw("hexwar-lab-v14", b.clone(), 0);
    assert_eq!(validate(&mut s).unwrap_err(), "at most one Universal per side");
    // One per side is fine.
    b.pop();
    b.push(p(3, -3, Kind::Universal, 1, 0));
    let mut s = raw("hexwar-lab-v14", b, 0);
    assert!(validate(&mut s).is_ok());
}

#[test]
fn v14_catalog_and_material_contracts() {
    let c = process(json!({"op":"catalog","rules_version":"hexwar-lab-v14"})).unwrap();
    let u = c["pieces"].as_array().unwrap().iter().find(|x| x["kind"] == "universal").unwrap();
    assert_eq!(u["min_rules"], "hexwar-lab-v14");
    assert_eq!(u["movement"], "swap");
    assert_eq!(u["default_value"], 300);
    assert_eq!(u["directional"], false);
    let c13 = process(json!({"op":"catalog","rules_version":"hexwar-lab-v13"})).unwrap();
    assert_eq!(c13["pieces"].as_array().unwrap().len(), 32);
    assert!(!c13["pieces"].as_array().unwrap().iter().any(|x| x["kind"] == "universal"));
    let s14 = st("hexwar-lab-v14", board(), 0);
    let e = process(json!({"op":"evaluate","state":serde_json::to_value(&s14).unwrap()})).unwrap();
    assert_eq!(e["material_values"]["universal"], 300);
    let mut b = board();
    b.remove(0);
    let s13 = st("hexwar-lab-v13", b, 0);
    let e = process(json!({"op":"evaluate","state":serde_json::to_value(&s13).unwrap()})).unwrap();
    assert!(e["material_values"].get("universal").is_none());
    assert!(e["effective_config"]["material_values"].get("universal").is_none());
}

#[test]
fn v14_behaves_like_v13_without_universal() {
    // Pass rule and fan rooting carry over: a frozen king passes.
    let b = vec![
        p(0, 3, Kind::King, 0, 0),
        p(0, 2, Kind::Enchanter, 1, 3),
        p(0, -4, Kind::King, 1, 3),
    ];
    let s = st("hexwar-lab-v14", b, 0);
    assert_eq!(moves(&s), vec![action("pass", None, None, None)]);
    assert_eq!(deadline(&s), 84);
}

#[test]
fn v14_search_uses_universal_to_dodge_capture() {
    // Search returns a legal action with a Universal on the board.
    let s = st("hexwar-lab-v14", board(), 0);
    let r = process(json!({"op":"search","state":serde_json::to_value(&s).unwrap(),
        "depth":3,"node_budget":20000,"policy":"tactical","quiescence":true})).unwrap();
    let a: Action = serde_json::from_value(r["action"].clone()).unwrap();
    assert!(moves(&s).contains(&a));
}
