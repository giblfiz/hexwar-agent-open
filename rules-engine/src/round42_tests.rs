use super::*;
use serde_json::json;

fn state(ply: u64) -> State {
    State { rules_version:"hexwar-lab-v8".into(), radius:4, turn:0, ply,
        reserves:[0,0], history:vec![], board:vec![
            Piece {q:1,r:0,kind:Kind::King,side:0,facing:0},
            Piece {q:-1,r:0,kind:Kind::King,side:1,facing:3},
        ] }
}

#[test]
fn v8_clock_is_84_and_v7_stays_70() {
    let mut v8 = state(83);
    assert_eq!(outcome(&v8).status, "ongoing");
    v8.ply = 84;
    assert_eq!(outcome(&v8).reason, "deadline_equal");
    let mut v7 = v8.clone(); v7.rules_version = "hexwar-lab-v7".into(); v7.ply = 70;
    assert_eq!(outcome(&v7).reason, "deadline_equal");
}

#[test]
fn v8_catalogue_and_tertiary_tiebreak_are_available() {
    let mut s = state(84);
    s.board.push(Piece {q:2,r:0,kind:Kind::Scout,side:0,facing:0});
    s.board.push(Piece {q:-4,r:0,kind:Kind::Scout,side:1,facing:3});
    assert_eq!(outcome(&s).reason, "deadline_army_proximity");
    assert_eq!(process(json!({"op":"catalog"})).unwrap()["rules_version"], "hexwar-lab-v9");
}
