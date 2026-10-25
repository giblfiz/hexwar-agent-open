use super::*;
fn p(q: i32, r: i32, kind: Kind, side: u8) -> Piece {
    Piece { q, r, kind, side, facing: 0 }
}
fn state() -> State {
    State { rules_version:"hexwar-lab-v7".into(),radius:4,turn:0,ply:70,
        board:vec![p(-1,0,Kind::King,0),p(1,0,Kind::King,1),
                   p(-2,0,Kind::Pawn,0),p(4,0,Kind::Pawn,1)],
        reserves:[0,0],history:vec![] }
}

#[test]
fn army_proximity_is_exact_and_versioned() {
    let mut s = state();
    validate(&mut s).unwrap();
    assert_eq!(army_distances(&s),[3,5]);
    assert_eq!(terminal(&s).winner,Some(0));
    assert_eq!(terminal(&s).reason,"deadline_army_proximity");
    s.reserves = [0,80];
    assert_eq!(terminal(&s).winner,Some(0));
    s.rules_version = "hexwar-lab-v6".into();
    assert_eq!(terminal(&s).status,"draw");
    s.rules_version = "hexwar-lab-v7".into();
    s.board[3].q = 2;
    assert_eq!(terminal(&s).reason,"deadline_equal");
    assert_eq!(terminal(&s).status,"draw");
    s.ply=69;
    assert_eq!(terminal(&s).status,"ongoing");
    s.ply=71;
    assert!(validate(&mut s).is_err());
}

#[test]
fn terminal_priority_king_center_count_proximity() {
    let mut s=state();
    s.board.push(p(4,-1,Kind::Pawn,1));
    assert_eq!(terminal(&s).winner,Some(1));
    assert_eq!(terminal(&s).reason,"deadline_piece_count");
    assert!(search_evaluation(&s,"material",true)<0);
    s.board[0].q=0;
    assert_eq!(terminal(&s).winner,Some(0));
    assert_eq!(terminal(&s).reason,"deadline_proximity");
    assert!(search_evaluation(&s,"material",true)>0);
    s.board.remove(0);
    assert_eq!(terminal(&s).winner,Some(1));
    assert_eq!(terminal(&s).reason,"king_lost");
}

#[test]
fn near_deadline_tertiary_signal_and_color_symmetry() {
    let mut s=state();
    // A deliberately strong but legal total-distance difference crosses an
    // integer evaluation unit. Tiny differences can legitimately quantize.
    s.board=vec![p(-1,0,Kind::King,0),p(1,0,Kind::King,1)];
    let near=[(0,0),(0,1),(0,-1),(1,-1),(-1,1),(-2,0),(-2,1),(-1,-1),(0,-2),(1,-2),(2,-2),(2,-1)];
    let far=[(4,0),(4,-1),(4,-2),(4,-3),(4,-4),(3,-4),(2,-4),(1,-4),(0,-4),(-1,-3),(-2,-2),(-3,-1)];
    for (q,r) in near { s.board.push(p(q,r,Kind::Pawn,0)); }
    for (q,r) in far { s.board.push(p(q,r,Kind::Pawn,1)); }
    s.ply=69;
    validate(&mut s).unwrap();
    let score=search_evaluation(&s,"material",true);
    assert!(score>0);
    s.turn=1;
    assert_eq!(search_evaluation(&s,"material",true),-score);
    for p in &mut s.board { p.q=-p.q; p.r=-p.r; p.side=1-p.side; p.facing=(p.facing+3)%6; }
    s.turn=0;
    assert_eq!(search_evaluation(&s,"material",true),-score);
}

#[test]
fn terminal_evaluator_and_search_agree_on_tertiary_win() {
    let s=state();
    let evaluate=process(json!({"op":"evaluate","state":s})).unwrap();
    assert_eq!(evaluate["value"],WIN);
    let answer=process(json!({"op":"search","state":s,"depth":2})).unwrap();
    assert_eq!(answer["value"],WIN);
    assert!(answer["action"].is_null());
}
