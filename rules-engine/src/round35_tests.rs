use super::*;

fn p(q: i32, r: i32, kind: Kind, side: u8) -> Piece {
    Piece { q, r, kind, side, facing: 0 }
}
fn state(ply: u64) -> State {
    State { rules_version: "hexwar-lab-v6".into(), radius: 4, turn: 0,
        board: vec![p(-1,0,Kind::King,0), p(1,0,Kind::King,1)],
        reserves: [0,0], ply, history: vec![] }
}

#[test]
fn deadline_and_count_tie_are_versioned() {
    let mut s = state(69);
    s.board.push(p(-3,0,Kind::Pawn,0));
    validate(&mut s).unwrap();
    assert_eq!(terminal(&s).status,"ongoing");
    s.ply = 70;
    assert_eq!(terminal(&s).winner,Some(0));
    assert_eq!(terminal(&s).reason,"deadline_piece_count");
    s.reserves = [0,20];
    assert_eq!(terminal(&s).winner,Some(0));
    s.board.push(p(3,0,Kind::Queen,1));
    assert_eq!(terminal(&s).status,"draw");
    s.ply = 71;
    assert!(validate(&mut s).is_err());
    s.rules_version = "hexwar-lab-v5".into();
    assert!(validate(&mut s).is_ok());
    s.board.pop();
    s.ply = 100;
    assert_eq!(terminal(&s).status,"draw");
}

#[test]
fn center_and_king_loss_outrank_bodies() {
    let mut s = state(70);
    s.board[0].q = 0;
    s.board.push(p(3,0,Kind::Queen,1));
    s.board.push(p(3,-1,Kind::Ghost,1));
    assert_eq!(terminal(&s).winner,Some(0));
    assert_eq!(terminal(&s).reason,"deadline_proximity");
    s.board.remove(0);
    assert_eq!(terminal(&s).winner,Some(1));
    assert_eq!(terminal(&s).reason,"king_lost");
    s.board.retain(|p| p.kind != Kind::King);
    assert_eq!(terminal(&s).reason,"both_kings_lost");
}

#[test]
fn final_capture_precedes_count() {
    let mut s = state(69);
    s.board.push(p(2,0,Kind::Guard,0));
    let a = action("move",Some([2,0]),Some([1,0]),None);
    assert!(moves(&s).contains(&a));
    let next = apply_unchecked(&s,&a);
    assert_eq!(next.ply,70);
    assert_eq!(terminal(&next).reason,"king_lost");
    assert_eq!(terminal(&next).winner,Some(0));
}

#[test]
fn late_body_value_is_equal_and_color_symmetric() {
    let mut s = state(70);
    s.board.push(p(-3,0,Kind::Pawn,0));
    let pawn = search_evaluation(&s,"material",true);
    assert!(pawn > 0);
    s.board[2].kind = Kind::Queen;
    assert_eq!(search_evaluation(&s,"material",true),pawn);
    s.reserves = [80,0];
    assert_eq!(search_evaluation(&s,"material",true),pawn);
    s.turn = 1;
    assert_eq!(search_evaluation(&s,"material",true),-pawn);
    s.turn = 0;
    s.ply = 0;
    assert_eq!(search_evaluation(&s,"material",true),evaluation_with(&s,"material",&MaterialValues::default()));
    s.ply = 69;
    let with_body = search_evaluation(&s,"material",true);
    s.board.pop();
    assert!(with_body > search_evaluation(&s,"material",true));
}

#[test]
fn center_objective_outweighs_maximum_body_disadvantage() {
    let mut s = state(70);
    s.radius = 5;
    s.board[0].q = 0;
    for q in -5..=5 { for r in -5..=5 {
        if distance(q,r) <= 5 && !s.board.iter().any(|p| p.q==q && p.r==r) {
            s.board.push(p(q,r,Kind::Pawn,1));
        }
    }}
    validate(&mut s).unwrap();
    assert!(search_evaluation(&s,"material",true)>0);
    assert_eq!(terminal(&s).winner,Some(0));
}

#[test]
fn quiescence_searches_quiet_deadline_action() {
    let s = state(69);
    let mut search = Search { material_values:MaterialValues::default(),nodes:0,budget:100000,
        policy:"material".into(),urgency:true,prefer_short_wins:false,quiescence:true,
        q_depth:1,q_nodes:0,q_cap_hits:0,max_q_ply:0 };
    let score = search.visit(&s,0,-WIN-1,WIN+1,0).unwrap();
    // Moving the king onto center wins; a stand-pat would be zero.
    assert_eq!(score,WIN);
    assert!(search.q_nodes > 1);
}

#[test]
fn v6_keeps_catalogue_and_friendly_ram() {
    let mut s = state(0);
    s.board.push(p(-3,0,Kind::Ram,0));
    s.board.push(p(-3,1,Kind::Hound,0));
    validate(&mut s).unwrap();
    assert!(moves(&s).iter().any(|a| a.kind == "push"));
    let response = process(json!({"op":"evaluate","state":s})).unwrap();
    assert!(response["effective_config"]["material_values"]["cricket"].is_number());
    assert_eq!(process(json!({"op":"catalog"})).unwrap()["rules_version"],"hexwar-lab-v9");
}
