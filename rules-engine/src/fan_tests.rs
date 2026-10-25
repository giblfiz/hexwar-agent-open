//! hexwar-lab-v12-fan: the Enchanter roots only its forward Scout fan.
use super::*;

fn p(q: i32, r: i32, kind: Kind, side: u8, facing: u8) -> Piece {
    Piece { q, r, kind, side, facing }
}
fn st(version: &str, mut pieces: Vec<Piece>, turn: u8) -> State {
    pieces.push(p(0, 3, Kind::King, 0, 0));
    pieces.push(p(0, -4, Kind::King, 1, 3));
    let mut s = State {
        rules_version: version.into(), radius: 4, board: pieces,
        turn, reserves: [0, 0], ply: 0, history: vec![],
    };
    validate(&mut s).unwrap();
    s
}
fn piece<'a>(s: &'a State, q: i32, r: i32) -> &'a Piece {
    &s.board[at(s, [q, r]).unwrap()]
}
// White Enchanter at the origin. With facing 0 its fan is DIRS 0, 1, 5:
// (0,-1), (1,-1), (-1,0). Side neighbours (1,0), (-1,1); rear (0,1).
fn ring(version: &str, facing: u8) -> State {
    st(version, vec![
        p(0, 0, Kind::Enchanter, 0, facing),
        p(0, -1, Kind::Guard, 1, 3),
        p(1, -1, Kind::Guard, 1, 3),
        p(-1, 0, Kind::Guard, 1, 3),
        p(1, 0, Kind::Guard, 1, 3),
        p(-1, 1, Kind::Guard, 1, 3),
        p(0, 1, Kind::Guard, 1, 3),
    ], 1)
}
const FAN0: [[i32; 2]; 3] = [[0, -1], [1, -1], [-1, 0]];
const OTHER0: [[i32; 2]; 3] = [[1, 0], [-1, 1], [0, 1]];

#[test]
fn v12_fan_roots_forward_three_only() {
    let s = ring("hexwar-lab-v12-fan", 0);
    for h in FAN0 {
        assert!(rooted(&s, piece(&s, h[0], h[1])), "fan hex {:?} must be rooted", h);
    }
    for h in OTHER0 {
        assert!(!rooted(&s, piece(&s, h[0], h[1])), "non-fan hex {:?} must be free", h);
    }
    assert_eq!(rooted_count(&s, 1), 3);
    let a = moves(&s);
    for h in FAN0 {
        assert!(!a.iter().any(|x| x.from == Some(h)));
    }
    // Rotating the Enchanter rotates the fan: facing 3 covers DIRS 2, 3, 4.
    let s = ring("hexwar-lab-v12-fan", 3);
    for h in OTHER0 {
        assert!(rooted(&s, piece(&s, h[0], h[1])), "facing-3 fan hex {:?}", h);
    }
    for h in FAN0 {
        assert!(!rooted(&s, piece(&s, h[0], h[1])));
    }
}
#[test]
fn v12_fan_rear_and_side_enemies_move_and_capture_enchanter() {
    let s = ring("hexwar-lab-v12-fan", 0);
    let a = moves(&s);
    for h in OTHER0 {
        assert!(a.iter().any(|x| x.from == Some(h) && x.kind == "move" && x.to != Some([0, 0])),
            "{:?} should have ordinary moves", h);
        assert!(a.contains(&action("move", Some(h), Some([0, 0]), None)),
            "{:?} should be able to capture the Enchanter", h);
        assert!(can_capture(&s, piece(&s, h[0], h[1]), [0, 0]));
    }
    let n = apply_unchecked(&s, &action("move", Some([0, 1]), Some([0, 0]), None));
    let k = piece(&n, 0, 0);
    assert_eq!((k.kind, k.side), (Kind::Guard, 1));
    assert!(!n.board.iter().any(|x| x.kind == Kind::Enchanter));
}
#[test]
fn v12_behaviour_unchanged_all_six_rooted() {
    let s = ring("hexwar-lab-v12", 0);
    assert_eq!(rooted_count(&s, 1), 6);
    let a = moves(&s);
    for h in FAN0.iter().chain(OTHER0.iter()) {
        assert!(!a.iter().any(|x| x.from == Some(*h)));
        assert!(!can_capture(&s, piece(&s, h[0], h[1]), [0, 0]));
    }
}
#[test]
fn v12_fan_is_accepted_wherever_v12_is() {
    let s = ring("hexwar-lab-v12-fan", 0);
    assert!(timed(&s));
    assert_eq!(deadline(&s), 84);
    let catalog = process(json!({"op":"catalog", "rules_version":"hexwar-lab-v12-fan"})).unwrap();
    assert!(catalog["pieces"].as_array().unwrap().iter().any(|p| p["kind"] == "enchanter"));
    // Search responses keep the V12 material table (Enchanter/Exchanger present).
    let v12 = serde_json::to_value(&ring("hexwar-lab-v12", 0)).unwrap();
    let fan = serde_json::to_value(&s).unwrap();
    let r12 = process(json!({"op":"search","state":v12,"node_budget":200,"depth":2})).unwrap();
    let rfan = process(json!({"op":"search","state":fan,"node_budget":200,"depth":2})).unwrap();
    assert_eq!(r12["material_values"], rfan["material_values"]);
    assert!(rfan["material_values"].get("enchanter").is_some());
}
