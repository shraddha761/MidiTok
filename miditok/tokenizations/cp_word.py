from math import ceil
from typing import List, Tuple, Dict, Optional, Union, Any
from pathlib import Path
import warnings

import numpy as np
from miditoolkit import MidiFile, Instrument, Note, TempoChange, TimeSignature

from ..midi_tokenizer import MIDITokenizer, _in_as_seq
from ..classes import TokSequence, Event
from ..constants import TIME_DIVISION, TEMPO, MIDI_INSTRUMENTS, TIME_SIGNATURE


class CPWord(MIDITokenizer):
    r"""Introduced with the
    `Compound Word Transformer (Hsiao et al.) <https://ojs.aaai.org/index.php/AAAI/article/view/16091>`_,
    this tokenization is similar to :ref:`REMI` but uses embedding pooling operations to reduce
    the overall sequence length: note tokens (*Pitch*, *Velocity* and *Duration*) are first
    independently converted to embeddings which are then merged (pooled) into a single one.
    Each compound token will be a list of the form (index: Token type):
    * 0: Family
    * 1: Bar/Position
    * 2: Pitch
    * 3: Velocity
    * 4: Duration
    * (+ Optional) Program: associated with notes (pitch/velocity/duration) or chords
    * (+ Optional) Chord: chords occurring with position tokens
    * (+ Optional) Rest: rest acting as a TimeShift token
    * (+ Optional) Tempo: occurring with position tokens
    * (+ Optional) TimeSig: occurring with bar tokens

    The output hidden states of the model will then be fed to several output layers
    (one per token type). This means that the training requires to add multiple losses.
    For generation, the decoding implies sample from several distributions, which can be
    very delicate. Hence, we do not recommend this tokenization for generation with small models.
    **Note:** When decoding multiple token sequences (of multiple tracks), i.e. when `config.use_programs` is False,
    only the tempos and time signatures of the first sequence will be decoded for the whole MIDI.
    """

    def _tweak_config_before_creating_voc(self):
        if self.config.use_time_signatures and self.config.use_rests:
            # NOTE: this configuration could work by adding a Bar token with the new TimeSig after the Rest, but the
            # decoding should handle this to not add another bar. Or it could work by making Rests not crossing new
            # bars. Rests would have a maximal value corresponding to the difference between the previous event tick
            # and the tick of the next bar. However, in cases of long rests of more than one bar, we would have
            # successions of Rest --> Bar --> Rest --> Bar ... tokens.
            warnings.warn(
                "You are using both Time Signatures and Rests with CPWord. Be aware that this configuration"
                "can result in altered time, as the time signature is carried by the Bar tokens, that are"
                "skipped during rests. To disable this warning, you can disable either Time Signatures or"
                "Rests. Otherwise, you can check that your data does not have time signature changes"
                "occurring during rests."
            )
        self.config.use_sustain_pedals = False
        self.config.use_pitch_bends = False
        self.config.program_changes = False
        token_types = ["Family", "Position", "Pitch", "Velocity", "Duration"]
        for add_tok_attr, add_token in [
            ("use_programs", "Program"),
            ("use_chords", "Chord"),
            ("use_rests", "Rest"),
            ("use_tempos", "Tempo"),
            ("use_time_signatures", "TimeSig"),
        ]:
            if getattr(self.config, add_tok_attr):
                token_types.append(add_token)
        self.vocab_types_idx = {
            type_: idx for idx, type_ in enumerate(token_types)
        }  # used for data augmentation
        self.vocab_types_idx["Bar"] = 1  # same as position

    def _add_time_events(self, events: List[Event]) -> List[List[Event]]:
        r"""
        Takes a sequence of note events (containing optionally Chord, Tempo and TimeSignature tokens),
        and insert (not inplace) time tokens (TimeShift, Rest) to complete the sequence.

        :param events: note events to complete.
        :return: the same events, with time events inserted.
        """
        time_division = self._current_midi_metadata["time_division"]
        ticks_per_sample = time_division / max(self.config.beat_res.values())

        # Add time events
        all_events = []
        current_bar = -1
        bar_at_last_ts_change = 0
        previous_tick = -1
        previous_note_end = 0
        tick_at_last_ts_change = tick_at_current_bar = 0
        current_time_sig = TIME_SIGNATURE
        if self.config.log_tempos:
            # pick the closest to the default value
            current_tempo = float(self.tempos[(np.abs(self.tempos - TEMPO)).argmin()])
        else:
            current_tempo = TEMPO
        current_program = None
        ticks_per_bar = self._compute_ticks_per_bar(
            TimeSignature(*current_time_sig, 0), time_division
        )
        # First look for a TimeSig token, if any is given at tick 0, to update current_time_sig
        if self.config.use_time_signatures:
            for event in events:
                if event.type == "TimeSig":
                    current_time_sig = list(map(int, event.value.split("/")))
                    ticks_per_bar = self._compute_ticks_per_bar(
                        TimeSignature(*current_time_sig, event.time), time_division
                    )
                    break
                elif event.type in [
                    "Pitch",
                    "Velocity",
                    "Duration",
                    "PitchBend",
                    "Pedal",
                ]:
                    break
        # Then look for a Tempo token, if any is given at tick 0, to update current_tempo
        if self.config.use_tempos:
            for event in events:
                if event.type == "Tempo":
                    current_tempo = event.value
                    break
                elif event.type in [
                    "Pitch",
                    "Velocity",
                    "Duration",
                    "PitchBend",
                    "Pedal",
                ]:
                    break
        # Add the time events
        for e, event in enumerate(events):
            if event.type == "Tempo":
                current_tempo = event.value
            elif event.type == "Program":
                current_program = event.value
                continue
            if event.time != previous_tick:
                # (Rest)
                if (
                    self.config.use_rests
                    and event.time - previous_note_end >= self._min_rest
                ):
                    previous_tick = previous_note_end
                    rest_values = self._ticks_to_duration_tokens(
                        event.time - previous_tick, rest=True
                    )
                    # Add Rest events and increment previous_tick
                    for dur_value, dur_ticks in zip(*rest_values):
                        all_events.append(
                            self.__create_cp_token(
                                previous_tick,
                                rest=".".join(map(str, dur_value)),
                                desc=f"{event.time - previous_tick} ticks",
                            )
                        )
                        previous_tick += dur_ticks
                    # We update current_bar and tick_at_current_bar here without creating Bar tokens
                    real_current_bar = (
                        bar_at_last_ts_change
                        + (previous_tick - tick_at_last_ts_change) // ticks_per_bar
                    )
                    if real_current_bar > current_bar:
                        tick_at_current_bar += (
                            real_current_bar - current_bar
                        ) * ticks_per_bar
                        current_bar = real_current_bar

                # Bar
                nb_new_bars = (
                    bar_at_last_ts_change
                    + (event.time - tick_at_last_ts_change) // ticks_per_bar
                    - current_bar
                )
                if nb_new_bars >= 1:
                    if self.config.use_time_signatures:
                        time_sig_arg = f"{current_time_sig[0]}/{current_time_sig[1]}"
                    else:
                        time_sig_arg = None
                    for i in range(nb_new_bars):
                        # exception when last bar and event.type == "TimeSig"
                        if i == nb_new_bars - 1 and event.type == "TimeSig":
                            time_sig_arg = list(map(int, event.value.split("/")))
                            time_sig_arg = f"{time_sig_arg[0]}/{time_sig_arg[1]}"
                        all_events.append(
                            self.__create_cp_token(
                                (current_bar + i + 1) * ticks_per_bar,
                                bar=True,
                                desc="Bar",
                                time_signature=time_sig_arg,
                            )
                        )
                    current_bar += nb_new_bars
                    tick_at_current_bar = (
                        tick_at_last_ts_change
                        + (current_bar - bar_at_last_ts_change) * ticks_per_bar
                    )

                # Position
                if event.type != "TimeSig":
                    pos_index = int(
                        (event.time - tick_at_current_bar) / ticks_per_sample
                    )
                    all_events.append(
                        self.__create_cp_token(
                            event.time,
                            pos=pos_index,
                            chord=event.value if event.type == "Chord" else None,
                            tempo=current_tempo if self.config.use_tempos else None,
                            desc="Position",
                        )
                    )

                previous_tick = event.time

            # Update time signature time variables, after adjusting the time (above)
            if event.type == "TimeSig":
                current_time_sig = list(map(int, event.value.split("/")))
                bar_at_last_ts_change += (
                    event.time - tick_at_last_ts_change
                ) // ticks_per_bar
                tick_at_last_ts_change = event.time
                ticks_per_bar = self._compute_ticks_per_bar(
                    TimeSignature(*current_time_sig, event.time), time_division
                )
                # We decrease the previous tick so that a Position token is enforced for the next event
                previous_tick -= 1

            # Convert event to CP Event
            # Update max offset time of the notes encountered
            if event.type == "Pitch" and e + 2 < len(events):
                all_events.append(
                    self.__create_cp_token(
                        event.time,
                        pitch=event.value,
                        vel=events[e + 1].value,
                        dur=events[e + 2].value,
                        program=current_program,
                    )
                )
                previous_note_end = max(previous_note_end, event.desc)
            elif event.type in [
                "Program",
                "Tempo",
                "TimeSig",
                "Chord",
            ]:
                previous_note_end = max(previous_note_end, event.time)

        return all_events

    def __create_cp_token(
        self,
        time: int,
        bar: bool = False,
        pos: int = None,
        pitch: int = None,
        vel: int = None,
        dur: str = None,
        chord: str = None,
        rest: str = None,
        tempo: float = None,
        time_signature: str = None,
        program: int = None,
        desc: str = "",
    ) -> List[Event]:
        r"""Create a CP Word token, with the following structure:
            (index. Token type)
            0. Family
            1. Bar/Position
            2. Pitch
            3. Velocity
            4. Duration
            (5. Program) optional, associated with notes (pitch/velocity/duration) or chords
            (6. Chord) optional, chords occurring with position tokens
            (7. Rest) optional, rest acting as a TimeShift token
            (8. Tempo) optional, occurring with position tokens
            (9. TimeSig) optional, occurring with bar tokens
        NOTE: the first Family token (first in list) will be given as an Event object to keep track
        of time easily so that other method can sort CP tokens afterwards.

        :param time: the current tick
        :param bar: True if this token represents a new bar occurring
        :param pos: the position index
        :param pitch: note pitch
        :param vel: note velocity
        :param dur: note duration
        :param chord: chord value
        :param rest: rest value
        :param tempo: tempo index
        :param program: a program number if you want to produce a Program CP token (read note above)
        :param desc: an optional argument for debug and used to spot position tokens in track_to_tokens
        :return: The compound token as a list of integers
        """

        def create_event(type_: str, value) -> Event:
            return Event(type=type_, value=value, time=time, desc=desc)

        cp_token = [
            Event(type="Family", value="Metric", time=time, desc=desc),
            Event(type="Ignore", value="None", time=time, desc=desc),
            Event(type="Ignore", value="None", time=time, desc=desc),
            Event(type="Ignore", value="None", time=time, desc=desc),
            Event(type="Ignore", value="None", time=time, desc=desc),
        ]
        for add_tok_attr in [
            "use_programs",
            "use_chords",
            "use_rests",
            "use_tempos",
            "use_time_signatures",
        ]:
            if getattr(self.config, add_tok_attr):
                cp_token.append(create_event("Ignore", "None"))

        if bar:
            cp_token[1] = create_event("Bar", "None")
            if time_signature is not None:
                cp_token[self.vocab_types_idx["TimeSig"]] = create_event(
                    "TimeSig", time_signature
                )
        elif pos is not None:
            cp_token[1] = create_event("Position", pos)
            if chord is not None:
                cp_token[self.vocab_types_idx["Chord"]] = create_event("Chord", chord)
            if tempo is not None:
                cp_token[self.vocab_types_idx["Tempo"]] = create_event("Tempo", tempo)
        elif rest is not None:
            cp_token[self.vocab_types_idx["Rest"]] = create_event("Rest", rest)
        elif pitch is not None:
            cp_token[0].value = "Note"
            cp_token[2] = create_event("Pitch", pitch)
            cp_token[3] = create_event("Velocity", vel)
            cp_token[4] = create_event("Duration", dur)
            if program is not None:
                cp_token[self.vocab_types_idx["Program"]] = create_event(
                    "Program", program
                )

        return cp_token

    @_in_as_seq()
    def tokens_to_midi(
        self,
        tokens: Union[
            Union[TokSequence, List, np.ndarray, Any],
            List[Union[TokSequence, List, np.ndarray, Any]],
        ],
        programs: Optional[List[Tuple[int, bool]]] = None,
        output_path: Optional[str] = None,
        time_division: int = TIME_DIVISION,
    ) -> MidiFile:
        r"""Converts tokens (:class:`miditok.TokSequence`) into a MIDI and saves it.

        :param tokens: tokens to convert. Can be either a list of :class:`miditok.TokSequence`,
        :param programs: programs of the tracks. If none is given, will default to piano, program 0. (default: None)
        :param output_path: path to save the file. (default: None)
        :param time_division: MIDI time division / resolution, in ticks/beat (of the MIDI to create).
        :return: the midi object (:class:`miditoolkit.MidiFile`).
        """
        # Unsqueeze tokens in case of one_token_stream
        if self.one_token_stream:  # ie single token seq
            tokens = [tokens]
        for i in range(len(tokens)):
            tokens[i] = tokens[i].tokens
        midi = MidiFile(ticks_per_beat=time_division)
        assert (
            time_division % max(self.config.beat_res.values()) == 0
        ), f"Invalid time division, please give one divisible by {max(self.config.beat_res.values())}"
        ticks_per_sample = time_division // max(self.config.beat_res.values())

        # RESULTS
        instruments: Dict[int, Instrument] = {}
        tempo_changes = [TempoChange(TEMPO, -1)]
        time_signature_changes = []

        def check_inst(prog: int):
            if prog not in instruments.keys():
                instruments[prog] = Instrument(
                    program=0 if prog == -1 else prog,
                    is_drum=prog == -1,
                    name="Drums" if prog == -1 else MIDI_INSTRUMENTS[prog]["name"],
                )

        current_tick = tick_at_last_ts_change = tick_at_current_bar = 0
        current_bar = -1
        bar_at_last_ts_change = 0
        current_program = 0
        current_instrument = None
        previous_note_end = 0
        for si, seq in enumerate(tokens):
            # First look for the first time signature if needed
            if si == 0:
                if self.config.use_time_signatures:
                    for compound_token in seq:
                        token_family = compound_token[0].split("_")[1]
                        if token_family == "Metric":
                            bar_pos = compound_token[1].split("_")[0]
                            if bar_pos == "Bar":
                                num, den = self._parse_token_time_signature(
                                    compound_token[
                                        self.vocab_types_idx["TimeSig"]
                                    ].split("_")[1]
                                )
                                time_signature_changes.append(
                                    TimeSignature(num, den, 0)
                                )
                                break
                        else:
                            break
                if len(time_signature_changes) == 0:
                    time_signature_changes.append(TimeSignature(*TIME_SIGNATURE, 0))
            current_time_sig = time_signature_changes[0]
            ticks_per_bar = self._compute_ticks_per_bar(current_time_sig, time_division)
            # Set track / sequence program if needed
            if not self.one_token_stream:
                current_tick = tick_at_last_ts_change = tick_at_current_bar = 0
                current_bar = -1
                bar_at_last_ts_change = 0
                previous_note_end = 0
                is_drum = False
                if programs is not None:
                    current_program, is_drum = programs[si]
                current_instrument = Instrument(
                    program=current_program,
                    is_drum=is_drum,
                    name="Drums"
                    if current_program == -1
                    else MIDI_INSTRUMENTS[current_program]["name"],
                )

            # Decode tokens
            for ti, compound_token in enumerate(seq):
                token_family = compound_token[0].split("_")[1]
                if token_family == "Note":
                    pad_range_idx = 6 if self.config.use_programs else 5
                    if any(
                        tok.split("_")[1] == "None"
                        for tok in compound_token[2:pad_range_idx]
                    ):
                        continue
                    pitch = int(compound_token[2].split("_")[1])
                    vel = int(compound_token[3].split("_")[1])
                    duration = self._token_duration_to_ticks(
                        compound_token[4].split("_")[1], time_division
                    )
                    if self.config.use_programs:
                        current_program = int(compound_token[5].split("_")[1])
                    new_note = Note(vel, pitch, current_tick, current_tick + duration)
                    if self.one_token_stream:
                        check_inst(current_program)
                        instruments[current_program].notes.append(new_note)
                    else:
                        current_instrument.notes.append(new_note)
                    previous_note_end = max(previous_note_end, current_tick + duration)

                elif token_family == "Metric":
                    bar_pos = compound_token[1].split("_")[0]
                    if bar_pos == "Bar":
                        current_bar += 1
                        if current_bar > 0:
                            current_tick = tick_at_current_bar + ticks_per_bar
                        tick_at_current_bar = current_tick
                        # Add new TS only if different from the last one
                        if self.config.use_time_signatures:
                            num, den = self._parse_token_time_signature(
                                compound_token[self.vocab_types_idx["TimeSig"]].split(
                                    "_"
                                )[1]
                            )
                            if (
                                num != current_time_sig.numerator
                                or den != current_time_sig.denominator
                            ):
                                current_time_sig = TimeSignature(num, den, current_tick)
                                if si == 0:
                                    time_signature_changes.append(current_time_sig)
                                tick_at_last_ts_change = tick_at_current_bar
                                bar_at_last_ts_change = current_bar
                                ticks_per_bar = self._compute_ticks_per_bar(
                                    current_time_sig, time_division
                                )
                    elif bar_pos == "Position":  # i.e. its a position
                        if current_bar == -1:
                            # in case this Position token comes before any Bar token
                            current_bar = 0
                        current_tick = (
                            tick_at_current_bar
                            + int(compound_token[1].split("_")[1]) * ticks_per_sample
                        )
                        # Add new tempo change only if different from the last one
                        if self.config.use_tempos and si == 0:
                            tempo = float(
                                compound_token[self.vocab_types_idx["Tempo"]].split(
                                    "_"
                                )[1]
                            )
                            if (
                                tempo != tempo_changes[-1].tempo
                                and current_tick != tempo_changes[-1].time
                            ):
                                tempo_changes.append(TempoChange(tempo, current_tick))
                    elif (
                        self.config.use_rests
                        and compound_token[self.vocab_types_idx["Rest"]].split("_")[1]
                        != "None"
                    ):
                        current_tick = max(previous_note_end, current_tick)
                        current_tick += self._token_duration_to_ticks(
                            compound_token[self.vocab_types_idx["Rest"]].split("_")[1],
                            time_division,
                        )
                        real_current_bar = (
                            bar_at_last_ts_change
                            + (current_tick - tick_at_last_ts_change) // ticks_per_bar
                        )
                        if real_current_bar > current_bar:
                            tick_at_current_bar += (
                                real_current_bar - current_bar
                            ) * ticks_per_bar
                            current_bar = real_current_bar

                    previous_note_end = max(previous_note_end, current_tick)

            # Add current_inst to midi and handle notes still active
            if not self.one_token_stream:
                midi.instruments.append(current_instrument)

        if len(tempo_changes) > 1:
            del tempo_changes[0]  # delete mocked tempo change
        tempo_changes[0].time = 0

        # create MidiFile
        if self.one_token_stream:
            midi.instruments = list(instruments.values())
        midi.tempo_changes = tempo_changes
        midi.time_signature_changes = time_signature_changes
        midi.max_tick = max(
            [
                max([note.end for note in track.notes]) if len(track.notes) > 0 else 0
                for track in midi.instruments
            ]
        )
        # Write MIDI file
        if output_path:
            Path(output_path).mkdir(parents=True, exist_ok=True)
            midi.dump(output_path)
        return midi

    def _create_base_vocabulary(self) -> List[List[str]]:
        r"""Creates the vocabulary, as a list of string tokens.
        Each token as to be given as the form of "Type_Value", separated with an underscore.
        Example: Pitch_58
        The :class:`miditok.MIDITokenizer` main class will then create the "real" vocabulary as
        a dictionary.
        Special tokens have to be given when creating the tokenizer, and
        will be added to the vocabulary by :class:`miditok.MIDITokenizer`.

        :return: the vocabulary as a list of string.
        """

        vocab = [[] for _ in range(5)]

        vocab[0].append("Family_Metric")
        vocab[0].append("Family_Note")

        # POSITION
        max_nb_beats = max(
            map(lambda ts: ceil(4 * ts[0] / ts[1]), self.time_signatures)
        )
        nb_positions = max(self.config.beat_res.values()) * max_nb_beats
        vocab[1].append("Ignore_None")
        vocab[1].append("Bar_None")
        vocab[1] += [f"Position_{i}" for i in range(nb_positions)]

        # PITCH
        vocab[2].append("Ignore_None")
        vocab[2] += [f"Pitch_{i}" for i in range(*self.config.pitch_range)]

        # VELOCITY
        vocab[3].append("Ignore_None")
        vocab[3] += [f"Velocity_{i}" for i in self.velocities]

        # DURATION
        vocab[4].append("Ignore_None")
        vocab[4] += [
            f'Duration_{".".join(map(str, duration))}' for duration in self.durations
        ]

        # PROGRAM
        if self.config.use_programs:
            vocab += [
                ["Ignore_None"]
                + [f"Program_{program}" for program in self.config.programs]
            ]

        # CHORD
        if self.config.use_chords:
            vocab += [["Ignore_None"] + self._create_chords_tokens()]

        # REST
        if self.config.use_rests:
            vocab += [
                ["Ignore_None"]
                + [f'Rest_{".".join(map(str, rest))}' for rest in self.rests]
            ]

        # TEMPO
        if self.config.use_tempos:
            vocab += [["Ignore_None"] + [f"Tempo_{i}" for i in self.tempos]]

        # TIME_SIGNATURE
        if self.config.use_time_signatures:
            vocab += [
                ["Ignore_None"]
                + [f"TimeSig_{i[0]}/{i[1]}" for i in self.time_signatures]
            ]

        return vocab

    def _create_token_types_graph(self) -> Dict[str, List[str]]:
        r"""Returns a graph (as a dictionary) of the possible token
        types successions.
        As with CP the tokens types are "merged", each state here corresponds to
        a "compound" token, which is characterized by the token types Program, Bar,
        Position/Chord/Tempo and Pitch/Velocity/Duration
        Here the combination of Pitch, Velocity and Duration tokens is represented by
        "Pitch" in the graph.
        NOTE: Program type is not referenced here, you can add it manually by
        modifying the tokens_types_graph class attribute following your strategy.

        :return: the token types transitions dictionary
        """
        dic = dict()

        dic["Bar"] = ["Position", "Bar"]
        dic["Position"] = ["Pitch"]
        dic["Pitch"] = ["Pitch", "Bar", "Position"]

        if self.config.use_chords:
            dic["Rest"] = ["Rest", "Position"]
            dic["Pitch"] += ["Rest"]

        if self.config.use_rests:
            dic["Rest"] = ["Rest", "Position", "Bar"]
            dic["Pitch"] += ["Rest"]

        if self.config.use_tempos:
            # Because a tempo change can happen at any moment
            dic["Position"] += ["Position", "Bar"]
            if self.config.use_rests:
                dic["Position"].append("Rest")
                dic["Rest"].append("Position")

        for key in dic:
            dic[key].append("Ignore")
        dic["Ignore"] = list(dic.keys())

        return dic

    @_in_as_seq()
    def tokens_errors(
        self, tokens: Union[TokSequence, List, np.ndarray, Any]
    ) -> Union[float, List[float]]:
        r"""Checks if a sequence of tokens is made of good token types
        successions and returns the error ratio (lower is better).
        The Pitch and Position values are also analyzed:
            - a position token cannot have a value <= to the current position (it would go back in time)
            - a pitch token should not be present if the same pitch is already played at the current position

        :param tokens: sequence of tokens to check
        :return: the error ratio (lower is better)
        """
        # If list of TokSequence -> recursive
        if isinstance(tokens, list):
            return [self.tokens_errors(tok_seq) for tok_seq in tokens]

        def cp_token_type(tok: List[int]) -> List[str]:
            family = self[0, tok[0]].split("_")[1]
            if family == "Note":
                return self[2, tok[2]].split("_")
            elif family == "Metric":
                bar_pos = self[1, tok[1]].split("_")
                if bar_pos[0] in ["Bar", "Position"]:
                    return bar_pos
                else:  # additional token
                    for i in range(1, 5):
                        decoded_token = self[-i, tok[-i]].split("_")
                        if decoded_token[0] != "Ignore":
                            return decoded_token
                raise RuntimeError("No token type found, unknown error")
            elif family == "None":
                return ["PAD", "None"]
            else:  # Program
                raise RuntimeError("No token type found, unknown error")

        tokens = tokens.ids
        err = 0
        previous_type = cp_token_type(tokens[0])[0]
        current_pos = -1
        program = 0
        current_pitches = {p: [] for p in self.config.programs}

        for token in tokens[1:]:
            token_type, token_value = cp_token_type(token)
            # Good token type
            if token_type in self.tokens_types_graph[previous_type]:
                if token_type == "Bar":  # reset
                    current_pos = -1
                    current_pitches = {p: [] for p in self.config.programs}
                elif token_type == "Pitch":
                    if self.config.use_programs:
                        program = int(self[5, token[5]].split("_")[1])
                    if int(token_value) in current_pitches[program]:
                        err += 1  # pitch already played at current position
                    else:
                        current_pitches[program].append(int(token_value))
                elif token_type == "Position":
                    if int(token_value) <= current_pos and previous_type != "Rest":
                        err += 1  # token position value <= to the current position
                    else:
                        current_pos = int(token_value)
                        current_pitches = {p: [] for p in self.config.programs}
            # Bad token type
            else:
                err += 1
            previous_type = token_type

        return err / len(tokens)
