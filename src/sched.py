#!/usr/bin/env python
# encoding: utf-8

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# author: Paco Nathan
# https://github.com/ceteri/exelixi


from json import dumps, loads
from service import ExecutorInfo, Framework, Worker
from threading import Thread
from uuid import uuid1
import os
import subprocess
import sys
import time

import mesos
import mesos_pb2


######################################################################
## class definitions


class MesosScheduler (mesos.Scheduler):
    # https://github.com/apache/mesos/blob/master/src/python/src/mesos.py

    def __init__ (self, executor, exe_path, n_exe, ff_name, prefix, cpu_alloc, mem_alloc):
        self.executor = executor
        self.taskData = {}
        self.tasksLaunched = 0
        self.tasksFinished = 0
        self.messagesSent = 0
        self.messagesReceived = 0

        # these protected members have been customized for Exelixi
        self._executors = {}
        self._exe_path = exe_path
        self._n_exe = n_exe
        self._ff_name = ff_name
        self._prefix = prefix
        self._cpu_alloc = cpu_alloc
        self._mem_alloc = mem_alloc


    def registered (self, driver, frameworkId, masterInfo):
        """
        Invoked when the scheduler successfully registers with a Mesos
        master. It is called with the frameworkId, a unique ID
        generated by the master, and the masterInfo which is
        information about the master itself.
        """

        print "registered with framework ID %s" % frameworkId.value


    def resourceOffers (self, driver, offers):
        """
        Invoked when resources have been offered to this framework. A
        single offer will only contain resources from a single slave.
        Resources associated with an offer will not be re-offered to
        _this_ framework until either (a) this framework has rejected
        those resources (see SchedulerDriver.launchTasks) or (b) those
        resources have been rescinded (see Scheduler.offerRescinded).
        Note that resources may be concurrently offered to more than
        one framework at a time (depending on the allocator being
        used).  In that case, the first framework to launch tasks
        using those resources will be able to use them while the other
        frameworks will have those resources rescinded (or if a
        framework has already launched tasks with those resources then
        those tasks will fail with a TASK_LOST status and a message
        saying as much).
        """

        print "got %d resource offers" % len(offers)

        for offer in offers:
            tasks = []
            print "got resource offer %s" % offer.id.value

            ## NB: currently we force 'offer.hostname' to be unique per Executor...
            ## could be changed, but we'd need to juggle port numbers

            if self.tasksLaunched < self._n_exe and offer.hostname not in self._executors:
                tid = self.tasksLaunched
                self.tasksLaunched += 1
                print "accepting offer on executor %s to start task %d" % (offer.hostname, tid)

                task = mesos_pb2.TaskInfo()
                task.task_id.value = str(tid)
                task.slave_id.value = offer.slave_id.value
                task.name = "task %d" % tid
                task.executor.MergeFrom(self.executor)

                cpus = task.resources.add()
                cpus.name = "cpus"
                cpus.type = mesos_pb2.Value.SCALAR
                cpus.scalar.value = self._cpu_alloc

                mem = task.resources.add()
                mem.name = "mem"
                mem.type = mesos_pb2.Value.SCALAR
                mem.scalar.value = self._mem_alloc

                tasks.append(task)
                self.taskData[task.task_id.value] = (offer.slave_id, task.executor.executor_id)

                ## NB: record/report slave state
                self._executors[offer.hostname] = ExecutorInfo(offer, task)

                for exe in self._executors.values():
                    print exe.report()

            # finally, have the driver launch the task
            driver.launchTasks(offer.id, tasks)


    def statusUpdate (self, driver, update):
        """
        Invoked when the status of a task has changed (e.g., a slave
        is lost and so the task is lost, a task finishes and an
        executor sends a status update saying so, etc.) Note that
        returning from this callback acknowledges receipt of this
        status update.  If for whatever reason the scheduler aborts
        during this callback (or the process exits) another status
        update will be delivered.  Note, however, that this is
        currently not true if the slave sending the status update is
        lost or fails during that time.
        """

        print "task %s is in state %d" % (update.task_id.value, update.state)

        if update.state == mesos_pb2.TASK_FINISHED:
            self.tasksFinished += 1

            slave_id, executor_id = self.taskData[update.task_id.value]
            self.messagesSent += 1

            # update the ExecutorInfo with details from the initial discovery task
            telemetry = loads(str(update.data))

            print "resource telemetry: slave %s, executor %s" % (slave_id.value, executor_id.value),
            print str(update.data)

            exe = self.lookup_executor(slave_id.value, executor_id.value)
            exe.ip_addr = telemetry["ip_addr"]

            ## NB: TODO make the Executor service port variable?
            exe.port = Worker.DEFAULT_PORT

            if self.tasksFinished == self._n_exe:
                print "init tasks completed"

            ## NB: TODO integrate service launch from tarball instead
            message = str(dumps([ self._exe_path, "-p", exe.port ]))
            driver.sendFrameworkMessage(executor_id, slave_id, message)


    def frameworkMessage (self, driver, executorId, slaveId, message):
        """
        Invoked when an executor sends a message. These messages are
        best effort; do not expect a framework message to be
        retransmitted in any reliable fashion.
        """

        self.messagesReceived += 1

        print "framework message: slave %s executor %s" % (slaveId.value, executorId.value)
        print "executor message %d received: %s" % (self.messagesReceived, str(message))

        if self.messagesReceived == self._n_exe:
            if self.messagesReceived != self.messagesSent:
                print "sent", self.messagesSent, "received", self.messagesReceived
                sys.exit(1)

            for exe in self._executors.values():
                print exe.report()

            print "all executors launched and init tasks completed"
            exe_info = self._executors.values()
            exe_list = [ exe.get_exe_uri() for exe in exe_info ]

            # run Framework orchestration via REST endpoints on the Executors
            fra = Framework(self._ff_name, self._prefix)
            fra.set_exe_list(exe_list, exe_info)

            time.sleep(1)
            fra.orchestrate()

            # shutdown the Executors after the end of an algorithm run
            driver.stop()


    def lookup_executor (self, slave_id, executor_id):
        """lookup the Executor based on IDs"""
        for exe in self._executors.values():
            if exe.slave_id == slave_id:
                return exe


    @staticmethod
    def start_framework (master_uri, exe_path, n_exe, ff_name, prefix, cpu_alloc, mem_alloc):
        # initialize an executor
        executor = mesos_pb2.ExecutorInfo()
        executor.executor_id.value = uuid1().hex
        executor.command.value = os.path.abspath(exe_path)
        executor.name = "Exelixi Executor"
        executor.source = "per-job build"

        # initialize the framework
        framework = mesos_pb2.FrameworkInfo()
        framework.user = "" # have Mesos fill in the current user
        framework.name = "Exelixi Framework"

        if os.getenv("MESOS_CHECKPOINT"):
            print "enabling checkpoint for the framework"
            framework.checkpoint = True
    
        ## NB: create a MesosScheduler and capture the command line options
        sched = MesosScheduler(executor, exe_path, n_exe, ff_name, prefix, cpu_alloc, mem_alloc)

        # initialize a driver
        if os.getenv("MESOS_AUTHENTICATE"):
            print "enabling authentication for the framework"
    
            if not os.getenv("DEFAULT_PRINCIPAL"):
                print "expecting authentication principal in the environment"
                sys.exit(1);

            if not os.getenv("DEFAULT_SECRET"):
                print "expecting authentication secret in the environment"
                sys.exit(1);

            credential = mesos_pb2.Credential()
            credential.principal = os.getenv("DEFAULT_PRINCIPAL")
            credential.secret = os.getenv("DEFAULT_SECRET")

            driver = mesos.MesosSchedulerDriver(sched, framework, master_uri, credential)
        else:
            driver = mesos.MesosSchedulerDriver(sched, framework, master_uri)

        return driver


    @staticmethod
    def stop_framework (driver):
        """ensure that the driver process terminates"""
        status = 0 if driver.run() == mesos_pb2.DRIVER_STOPPED else 1
        driver.stop();
        sys.exit(status)


class MesosExecutor (mesos.Executor):
    # https://github.com/apache/mesos/blob/master/src/python/src/mesos.py

    def launchTask (self, driver, task):
        """
        Invoked when a task has been launched on this executor
        (initiated via Scheduler.launchTasks).  Note that this task
        can be realized with a thread, a process, or some simple
        computation, however, no other callbacks will be invoked on
        this executor until this callback has returned.
        """

        # create a thread to run the task: tasks should always be run
        # in new threads or processes, rather than inside launchTask
        def run_task():
            print "requested task %s" % task.task_id.value

            update = mesos_pb2.TaskStatus()
            update.task_id.value = task.task_id.value
            update.state = mesos_pb2.TASK_RUNNING
            update.data = str("running discovery task")

            print update.data
            driver.sendStatusUpdate(update)

            update = mesos_pb2.TaskStatus()
            update.task_id.value = task.task_id.value
            update.state = mesos_pb2.TASK_FINISHED

            ## NB: TODO test port availability...
            update.data = str(dumps(get_telemetry(), indent=4))

            print update.data
            driver.sendStatusUpdate(update)

        # now run the requested task
        thread = Thread(target=run_task)
        thread.start()


    def frameworkMessage (self, driver, message):
        """
        Invoked when a framework message has arrived for this
        executor. These messages are best effort; do not expect a
        framework message to be retransmitted in any reliable fashion.
        """

        # launch service
        print "received message %s" % message
        subprocess.Popen(loads(message))

        # send the message back to the scheduler
        driver.sendFrameworkMessage(str("service launched"))


    @staticmethod
    def run_executor ():
        """run the executor until it is stopped externally by the framework"""
        driver = mesos.MesosExecutorDriver(MesosExecutor())
        sys.exit(0 if driver.run() == mesos_pb2.DRIVER_STOPPED else 1)


if __name__=='__main__':
    print "Starting executor..."
    MesosExecutor.run_executor()
