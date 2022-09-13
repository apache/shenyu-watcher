/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.shenyu.env;

import org.apache.commons.io.IOUtils;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/**
 * @author sinsy
 * @date 2022-09-08
 */
public class CheckEnv {

    private final static String PYTHON_VERSION = "Python 3.8";

    public static boolean PYTHON_CHECK = false;

    static {
        String exe = "python -V";
        Process process;
        try {
            process = Runtime.getRuntime().exec(exe);
            InputStream inputStream = process.getInputStream();
            String str = IOUtils.toString(inputStream, StandardCharsets.UTF_8);

            if (str.contains(PYTHON_VERSION)) {
                PYTHON_CHECK = true;
            }

        } catch (IOException e) {
            e.printStackTrace();
        }


    }

}
