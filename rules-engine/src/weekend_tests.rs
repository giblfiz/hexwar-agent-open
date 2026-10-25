use super::*;

#[test]
fn cleaver_attack_geometry_matches_moves_and_ignores_facing() {
    let mut s = State {
        rules_version: "hexwar-lab-v4".into(), radius: 3, turn: 0,
        reserves: [0,0], ply: 0, history: vec![],
        board: vec![Piece {q:0,r:0,kind:Kind::Cleaver,side:0,facing:0}],
    };
    for facing in 0..6 {
        s.board[0].facing = facing;
        let legal = moves(&s);
        assert_eq!(legal.len(), 12);
        for q in -3..=3 {
            for r in -3..=3 {
                if distance(q,r)>3 {continue;}
                assert_eq!(can_capture(&s, &s.board[0], [q,r]), legal.iter().any(|a| a.to==Some([q,r])));
            }
        }
    }
    s.board.push(Piece {q:1,r:0,kind:Kind::Guard,side:0,facing:0});
    assert!(!can_capture(&s, &s.board[0], [2,0]));
    assert!(!moves(&s).iter().any(|a| a.from==Some([0,0]) && a.to==Some([2,0])));
}
