"""Simulation-only state machine. No network or alarm outputs."""
import math
import time

class EntryDelay:
    def __init__(self, clock=time.monotonic, delay=60):
        self.clock = clock
        self.delay = delay
        self.state = 'unknown'
        self.deadline = None
        self.connected = True
        self.events = []

    def log(self, message):
        self.events.append(message)
        self.events = self.events[-20:]

    def update(self, event):
        self.tick()
        if event == 'confirmed_arm':
            if not self.connected or self.state not in ('unknown', 'disarmed'):
                raise ValueError('Reset or confirm disarmed before a new simulation.')
            self.state = 'armed'
            self.log('Simulated armed state confirmed.')
        elif event == 'door_open':
            if self.state == 'armed' and self.connected:
                self.deadline = self.clock() + self.delay
                self.state = 'entry_delay'
                self.log(f'Door opened: {self.delay}-second deadline started.')
            else:
                self.log('Door event did not start or extend a deadline.')
        elif event == 'disarm_requested':
            self.log('Disarm requested; waiting for confirmation. Timer continues.')
        elif event == 'confirmed_disarm':
            if not self.connected:
                raise ValueError('Cannot confirm disarm while connection is lost.')
            self.state = 'disarmed'
            self.deadline = None
            self.log('Simulated disarm confirmed; pending timer cancelled.')
        elif event == 'connection_lost':
            self.connected = False
            self.log('Connection lost. Pending deadline is preserved; state is unverified.')
        elif event == 'reset':
            self.__init__(self.clock, self.delay)
            self.log('Simulation reset. Actual alarm state remains unknown.')
        else:
            raise ValueError('Unknown simulation event.')
        return self.snapshot()

    def tick(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            self.deadline = None
            self.state = 'alarm_due'
            self.log('SIMULATION: alarm response would be due now. No live action sent.')

    def snapshot(self):
        self.tick()
        return {'state': self.state, 'connected': self.connected,
                'remaining': max(0, math.ceil(self.deadline - self.clock())) if self.deadline is not None else None,
                'events': self.events.copy(), 'simulation': True}
